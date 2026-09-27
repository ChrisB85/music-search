"""Fuzzy search over the Music Assistant library, served over HTTP for Home Assistant.

Music Assistant's own library search is a plain SQL LIKE, so speech-to-text output such as
"Marlina Mansona" or "Black Sabat" finds nothing. This service keeps the library in memory and
matches with rapidfuzz, plus per-artist phonetic aliases from aliases.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import random
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
EDITOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor.html")
ALIASES_HEADER = """\
# Phonetic spellings per Music Assistant artist, as Polish speech-to-text may write them.
# Key = artist name exactly as in the library. Edited by the app's editor panel and by the
# voice agent; hand edits apply without a restart. Unmatched queries land in misses.log.
"""
# Characters NFKD does not decompose.
_EXTRA = str.maketrans({"ł": "l", "ø": "o", "æ": "ae", "ß": "ss", "đ": "d"})

log = logging.getLogger("music_search")


def name_score(query: str, choice: str, **_kwargs) -> float:
    """Name similarity without WRatio's partial matching, which lets a short name hide inside a
    longer query ("Zenek Martyniuk" scored 75 against "Martyr"). A query that is a whole-word
    subset of the name ("Manson") still scores high."""
    return max(fuzz.ratio(query, choice), fuzz.token_sort_ratio(query, choice), 0.95 * fuzz.token_set_ratio(query, choice))


# The editor asks to speak the name the way one talks to the assistant ("Puść Judas Priest w pokoju"),
# so the transcript keeps the inflection STT really produces; strip the command words around it.
_COMMAND = re.compile(r"^(?:(?:puść|pusc|włącz|wlacz|zagraj|odtwórz|odtworz)\s+)?(.*?)(?:\s+w\s+pokoju)?$", re.I)


def spoken_name(transcript: str) -> str:
    text = transcript.strip().strip(".,!?;:\"'„”").strip()
    return _COMMAND.match(text).group(1).strip() if text else ""


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
        hits = process.extract(norm(query), self.artist_keys, scorer=name_score, processor=None, limit=limit * 4)
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
        self.stt_entity = options.get("stt_entity") or "stt.google_cloud"
        self.stt_language = options.get("stt_language") or "pl-PL"
        # person entity -> Music Assistant user; each user sees only their own libraries.
        self.users = {u["person"]: u["ma_user"] for u in options.get("users", [])}
        self.aliases_file = os.path.join(CONFIG_DIR, "aliases.yaml")
        self.misses_file = os.path.join(CONFIG_DIR, "misses.log")
        # Keyed by MA user; "" = every library (no username passed to Music Assistant).
        self.libraries: dict[str, dict[str, list[dict]]] = {}
        self.indexes: dict[str, Index] = {"": Index({}, {})}
        self.aliases_mtime = 0.0
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()

    @property
    def index(self) -> Index:
        return self.indexes[""]

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
        libraries = {}
        for user in ["", *self.users.values()]:
            library = {}
            for kind in TYPES:
                data = {"config_entry_id": entry, "media_type": kind, "limit": 1000000}
                if user:
                    data["username"] = user
                library[kind] = self.ha("POST", "/services/music_assistant/get_library?return_response", data)[
                    "service_response"
                ]["items"]
            libraries[user] = library
            log.info("Library loaded for %s: %s", user or "all users", {k: len(v) for k, v in library.items()})
        self.libraries = libraries
        self.rebuild()

    def rebuild(self) -> None:
        aliases = self.load_aliases()
        indexes = {user: Index(library, aliases) for user, library in self.libraries.items()}
        with self.lock:
            self.indexes = indexes

    def artist_names(self) -> list[str]:
        return sorted({a["name"] for a in self.libraries.get("", {}).get("artist", [])}, key=str.lower)

    def save_aliases(self, aliases: dict[str, list[str]]) -> None:
        clean = {k: sorted(set(v), key=str.lower) for k, v in aliases.items() if v}
        body = yaml.safe_dump(clean, allow_unicode=True, default_flow_style=None, sort_keys=True, width=10000)
        with self.write_lock:
            tmp = self.aliases_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(ALIASES_HEADER + body)
            os.replace(tmp, self.aliases_file)
        self.rebuild()

    def set_aliases(self, artist: str, aliases: list[str]) -> dict:
        """Replace one artist's aliases (the editor saves a whole row)."""
        if artist not in self.artist_names():
            return {"error": f"Nie ma artysty „{artist}” w bibliotece."}
        own = norm(artist)
        others = {norm(a): a for a in self.artist_names() if norm(a) != own}
        cleaned = []
        for alias in aliases:
            key = norm(alias)
            if not key or key == own:
                continue
            if key in others:
                return {"error": f"„{alias}” to nazwa innego artysty: {others[key]}."}
            cleaned.append(alias.strip())
        data = self.load_aliases()
        data[artist] = cleaned
        self.save_aliases(data)
        return {"artist": artist, "aliases": sorted(set(cleaned), key=str.lower)}

    def add_alias(self, artist: str, alias: str) -> dict:
        """Add one spelling; `artist` may itself be approximate (the voice agent sends it)."""
        names = self.artist_names()
        if artist not in names:
            found = self.index.find_artists(artist, 1)
            if not found or found[0][1] < self.min_score:
                return {"error": f"Nie ma artysty „{artist}” w bibliotece."}
            artist = found[0][0]["name"]
        current = self.load_aliases().get(artist) or []
        if norm(alias) in {norm(a) for a in current} or norm(alias) == norm(artist):
            return {"artist": artist, "aliases": current, "message": f"Zapis „{alias}” już jest przy {artist}."}
        result = self.set_aliases(artist, [*current, alias])
        if "error" not in result:
            result["message"] = f"Zapamiętane: „{alias}” to {artist}."
        return result

    def misses(self, limit: int = 100) -> list[str]:
        if not os.path.exists(self.misses_file):
            return []
        with open(self.misses_file, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f.readlines()[-limit:]][::-1]

    def transcribe(self, pcm: bytes) -> dict:
        """Raw 16 kHz mono 16-bit PCM from the editor's microphone, through the same HA STT the
        voice pipelines use, so the stored spelling is what speech-to-text really writes."""
        req = urllib.request.Request(
            f"{HA_URL}/stt/{self.stt_entity}",
            method="POST",
            data=pcm,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "X-Speech-Content": "format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; "
                f"language={self.stt_language}",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.load(resp)
        if result.get("result") != "success" or not result.get("text"):
            return {"error": "Nic nie rozpoznano."}
        return {"text": spoken_name(result["text"]), "transcript": result["text"]}

    def delete_miss(self, line: str) -> dict:
        with self.write_lock:
            lines = self.misses(limit=10**9)[::-1]
            if line not in lines:
                return {"error": "Nie ma takiego wpisu."}
            lines.remove(line)
            with open(self.misses_file, "w", encoding="utf-8") as f:
                f.writelines(entry + "\n" for entry in lines)
        return {"deleted": line}

    def resolve_user(self, agent: str = "", person: str = "") -> tuple[str, str]:
        """(MA user, person entity) whose library to use; ("", "") = all libraries.

        `person` names the library owner explicitly ("Aurelii" works: fuzzy on the person's name).
        Otherwise `agent` (the conversation agent that heard the command) maps to a person through
        the person_assistant sensors, which hold each person's pipeline and its conversation agent."""
        if not self.users or not (agent or person):
            return "", ""
        states = self.ha("GET", "/states")
        if person:
            names = {}
            for st in states:
                if st["entity_id"] in self.users:
                    names[norm(st["entity_id"].split(".", 1)[1])] = st["entity_id"]
                    names[norm(st["attributes"].get("friendly_name", ""))] = st["entity_id"]
            hit = process.extractOne(norm(person), list(names), scorer=name_score, processor=None)
            if hit and hit[1] >= self.min_score:
                return self.users[names[hit[0]]], names[hit[0]]
            return "", ""
        for st in states:
            attrs = st["attributes"]
            if attrs.get("conversation_engine") == agent and attrs.get("person") in self.users:
                return self.users[attrs["person"]], attrs["person"]
        return "", ""

    def random(self, kind: str, agent: str, person: str) -> dict:
        user, owner = self.resolve_user(agent, person)
        items = self.libraries.get(user, {}).get(kind) or []
        if not items:
            return {"item": None, "user": user, "person": owner}
        item = random.choice(items)
        artist = ", ".join(a["name"] for a in item.get("artists") or [])
        return {
            "item": {"type": kind, "name": item["name"], "artist": artist, "uri": item["uri"]},
            "user": user,
            "person": owner,
        }

    def search(self, query: str, artist: str, kind: str, limit: int, agent: str = "", person: str = "") -> dict:
        if os.path.exists(self.aliases_file) and os.path.getmtime(self.aliases_file) != self.aliases_mtime:
            self.rebuild()  # aliases.yaml edited by hand: pick it up without a restart
        user, owner = self.resolve_user(agent, person)
        with self.lock:
            results = self.indexes.get(user, self.indexes[""]).search(query, artist, kind, limit)
        best = results[0] if results and results[0]["score"] >= self.min_score else None
        if best is None:
            with open(self.misses_file, "a", encoding="utf-8") as f:
                top = f"{results[0]['name']} ({results[0]['score']})" if results else "-"
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\tq={query}\tartist={artist}\ttype={kind}\tuser={user}\ttop={top}\n")
        return {"best": best, "results": results, "user": user, "person": owner}

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
            agent, person = args.get("agent", ""), args.get("person", "")
            kind = args.get("type", "")
            if url.path in ("/", "/editor"):
                with open(EDITOR_FILE, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if url.path == "/api/aliases":
                return self.reply(
                    200,
                    {"aliases": service.load_aliases(), "artists": service.artist_names(), "misses": service.misses()},
                )
            if url.path == "/health":
                return self.reply(200, {user or "all": len(ix.artists) for user, ix in service.indexes.items()})
            if kind not in ("", *TYPES):
                return self.reply(400, {"error": f"unknown type {kind}"})
            if url.path == "/random":
                return self.reply(200, service.random(kind or "album", agent, person))
            if url.path != "/search" or not args.get("q"):
                return self.reply(400, {"error": "use /search?q=&artist=&type=&agent=&person= or /random?type=&agent=&person="})
            self.reply(200, service.search(args["q"], args.get("artist", ""), kind, int(args.get("limit", 5)), agent, person))

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/refresh":
                service.refresh()
                return self.reply(200, {"artists": len(service.index.artists)})
            length = int(self.headers.get("Content-Length") or 0)
            if path == "/api/stt":
                if not 0 < length <= 16000 * 2 * 15:  # at most 15 s of audio
                    return self.reply(400, {"error": "Nagranie puste albo za długie."})
                result = service.transcribe(self.rfile.read(length))
                return self.reply(400 if "error" in result else 200, result)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.reply(400, {"error": "body must be JSON"})
            if path == "/api/aliases":  # editor: replace one artist's aliases
                result = service.set_aliases(str(body.get("artist", "")), [str(a) for a in body.get("aliases", [])])
            elif path == "/api/misses/delete":
                result = service.delete_miss(str(body.get("line", "")))
            elif path == "/aliases/add":  # voice agent: add one spelling
                result = service.add_alias(str(body.get("artist", "")).strip(), str(body.get("alias", "")).strip())
            else:
                return self.reply(404, {"error": "not found"})
            self.reply(400 if "error" in result else 200, result)

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
