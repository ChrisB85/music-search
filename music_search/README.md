# Music Search

Fuzzy search over the Music Assistant library, so voice commands survive speech-to-text errors
("Marlina Mansona" → Marilyn Manson, "Metalika" → Metallica). Music Assistant's own library search
is a plain substring match.

The library is read from Home Assistant (`music_assistant.get_library`) at start and every
`refresh_hours`.

## API

Inside Home Assistant the app is reachable at `http://<hostname>:8098` (hostname shown on the app page).

- `GET /search?q=<title or artist>&artist=<artist>&type=artist|album|track&limit=5` →
  `{"best": {...} | null, "results": [{"type", "name", "artist", "uri", "score"}]}`.
  `best` is the top result when its score reaches `min_score`. Pass `uri` to `music_assistant.play_media`.
- `POST /refresh` — reload the library now.
- `GET /health`

## Files in `/addon_configs/<prefix>_music_search/`

- `aliases.yaml` — phonetic spellings per artist, as speech-to-text writes them. Edits apply
  immediately.
  ```yaml
  Iron Maiden: [Ajron Mejden]
  ```
- `misses.log` — queries with no confident match; use it to add aliases.
