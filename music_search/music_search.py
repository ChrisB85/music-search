"""Fuzzy search over the Music Assistant library, served over HTTP for Home Assistant.

Music Assistant's own library search is a plain SQL LIKE, so speech-to-text output such as
"Marlina Mansona" or "Black Sabat" finds nothing. This service keeps the library in memory and
matches with rapidfuzz, plus per-artist phonetic aliases from aliases.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml
from rapidfuzz import fuzz, process

HA_URL = os.environ.get("HA_URL", "http://supervisor/core/api")
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
PORT = int(os.environ.get("PORT", "8098"))

TYPES = ("artist", "album", "track")
# Characters NFKD does not decompose.
_EXTRA = str.maketrans({"ł": "l", "ø": "o", "æ": "ae", "ß": "ss", "đ": "d"})

log = logging.getLogger("music_search")


def norm(text: str) -> str:
    """Lowercase, strip diacritics and punctuation, so STT output and tags compare equal."""
    text = unicodedata.normalize("NFKD", text.lower().translate(_EXTRA))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


class Index:
    """In-memory library index. Pure: no I/O, so tests can build it from fixtures."""

    def __init__(self, library: dict[str, list[dict]], aliases: dict[str, list[str]]):
        alias_map = {norm(k): v or [] for k, v in (aliases or {}).items()}
        # Artist choice keys: the name plus every alias, all pointing at the same artist.
        self.artists: list[dict] = []
        self.artist_keys: list[str] = []
        self.artist_of_key: list[int] = []
        for item in library.get("artist", []):
            i = len(self.artists)
            self.artists.append({"type": "artist", "name": item["name"], "artist": item["name"], "uri": item["uri"]})
            for key in {norm(item["name"]), *(norm(a) for a in alias_map.get(norm(item["name"]), []))}:
                if key:
                    self.artist_keys.append(key)
                    self.artist_of_key.append(i)
        # Albums and tracks: title alone, and "title artist" for one-string queries.
        self.items: dict[str, list[dict]] = {}
        self.titles: dict[str, list[str]] = {}
        self.full: dict[str, list[str]] = {}
        for kind in ("album", "track"):
            rows, seen = [], set()
            for item in library.get(kind, []):
                artist = ", ".join(a["name"] for a in item.get("artists") or [])
                dedupe = (norm(item["name"]), norm(artist))
                if dedupe in seen:  # same song on several releases: first one is enough
                    continue
                seen.add(dedupe)
                rows.append({"type": kind, "name": item["name"], "artist": artist, "uri": item["uri"]})
            self.items[kind] = rows
            self.titles[kind] = [norm(r["name"]) for r in rows]
            self.full[kind] = [norm(f"{r['name']} {r['artist']}") for r in rows]

    def find_artists(self, query: str, limit: int) -> list[tuple[dict, float]]:
        best: dict[int, float] = {}
        hits = process.extract(norm(query), self.artist_keys, scorer=fuzz.WRatio, processor=None, limit=limit * 4)
        for _key, score, pos in hits:
            i = self.artist_of_key[pos]
            best[i] = max(best.get(i, 0), score)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:limit]
        return [(self.artists[i], s) for i, s in ranked]

    def search(self, query: str, artist: str = "", kind: str = "", limit: int = 5) -> list[dict]:
        if kind == "artist" or (not kind and not artist):
            found = [dict(a, score=s) for a, s in self.find_artists(query, limit)]
            if kind == "artist":
                return [dict(r, score=round(r["score"], 1)) for r in found]
        else:
            found = []
        kinds = [kind] if kind in ("album", "track") else ["album", "track"]
        for k in kinds:
            if artist:
                # Resolve the artist first, then match the title only among that artist's items.
                names = {norm(a["name"]): s for a, s in self.find_artists(artist, 3) if s >= 70}
                pool = [(i, names[norm(r["artist"])]) for i, r in enumerate(self.items[k]) if norm(r["artist"]) in names]
                q = norm(query)
                for i, artist_score in pool:
                    score = 0.7 * fuzz.WRatio(q, self.titles[k][i], processor=None) + 0.3 * artist_score
                    found.append(dict(self.items[k][i], score=score))
            else:
                # One string like "paranoid black sabbath": match "title artist", but the title itself
                # must appear in the query, or a bare artist name would pull in all their tracks.
                q = norm(query)
                hits = process.extract(q, self.full[k], scorer=fuzz.WRatio, processor=None, limit=limit * 20)
                words = q.split()
                for _c, score, i in hits:
                    title = self.titles[k][i]
                    # Very short titles ("I", "15") would partially match almost anything.
                    title_score = fuzz.partial_ratio(title, q) if len(title) >= 4 else 100 * (title in words)
                    found.append(dict(self.items[k][i], score=min(score, title_score)))
        # Ties: prefer artist over album over track (a bare name usually means the artist).
        found.sort(key=lambda r: (-round(r["score"], 1), TYPES.index(r["type"])))
        return [dict(r, score=round(r["score"], 1)) for r in found[:limit]]


class Service:
    """Loads the library from Home Assistant and keeps the index and aliases fresh."""

    def __init__(self, options: dict):
        self.min_score = float(options.get("min_score", 75))
        self.refresh_hours = float(options.get("refresh_hours", 6))
        self.aliases_file = os.path.join(CONFIG_DIR, "aliases.yaml")
        self.misses_file = os.path.join(CONFIG_DIR, "misses.log")
        self.library: dict[str, list[dict]] = {}
        self.aliases_mtime = 0.0
        self.index = Index({}, {})
        self.lock = threading.Lock()

    def ha(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            HA_URL + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    def load_aliases(self) -> dict:
        if not os.path.exists(self.aliases_file):
            return {}
        self.aliases_mtime = os.path.getmtime(self.aliases_file)
        with open(self.aliases_file, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def refresh(self) -> None:
        entry = self.ha("GET", "/config/config_entries/entry?domain=music_assistant")[0]["entry_id"]
        library = {}
        for kind in TYPES:
            data = {"config_entry_id": entry, "media_type": kind, "limit": 1000000}
            library[kind] = self.ha("POST", "/services/music_assistant/get_library?return_response", data)[
                "service_response"
            ]["items"]
        self.library = library
        self.rebuild()
        log.info("Library loaded: %s", {k: len(v) for k, v in library.items()})

    def rebuild(self) -> None:
        index = Index(self.library, self.load_aliases())
        with self.lock:
            self.index = index

    def search(self, query: str, artist: str, kind: str, limit: int) -> dict:
        if os.path.exists(self.aliases_file) and os.path.getmtime(self.aliases_file) != self.aliases_mtime:
            self.rebuild()  # aliases.yaml edited by hand: pick it up without a restart
        with self.lock:
            results = self.index.search(query, artist, kind, limit)
        best = results[0] if results and results[0]["score"] >= self.min_score else None
        if best is None:
            with open(self.misses_file, "a", encoding="utf-8") as f:
                top = f"{results[0]['name']} ({results[0]['score']})" if results else "-"
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\tq={query}\tartist={artist}\ttype={kind}\ttop={top}\n")
        return {"best": best, "results": results}

    def refresh_loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - keep serving the old index, retry next round
                log.exception("Library refresh failed")
            time.sleep(self.refresh_hours * 3600)


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            args = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/health":
                return self.reply(200, {"artists": len(service.index.artists)})
            if url.path != "/search" or not args.get("q"):
                return self.reply(400, {"error": "use /search?q=...&artist=...&type=artist|album|track"})
            kind = args.get("type", "")
            if kind not in ("", *TYPES):
                return self.reply(400, {"error": f"unknown type {kind}"})
            self.reply(200, service.search(args["q"], args.get("artist", ""), kind, int(args.get("limit", 5))))

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/refresh":
                return self.reply(404, {"error": "not found"})
            service.refresh()
            self.reply(200, {"artists": len(service.index.artists)})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = {}
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            options = json.load(f)
    service = Service(options)
    threading.Thread(target=service.refresh_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(service)).serve_forever()


if __name__ == "__main__":
    main()
