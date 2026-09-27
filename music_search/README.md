# Music Search

Fuzzy search over the Music Assistant library, so voice commands survive speech-to-text errors
("Marlina Mansona" → Marilyn Manson, "Metalika" → Metallica). Music Assistant's own library search
is a plain substring match.

The library is read from Home Assistant (`music_assistant.get_library`) at start and every
`refresh_hours`, once for all libraries and once per Music Assistant user in `users`:

```yaml
users:
  - person: person.krzysiek
    ma_user: krzysztof
```

## API

Inside Home Assistant the app is reachable at `http://<hostname>:8098` (hostname shown on the app page).

- `GET /search?q=<title or artist>&artist=<artist>&type=artist|album|track&limit=5&agent=&person=` →
  `{"best": {...} | null, "results": [{"type", "name", "artist", "uri", "score"}], "user", "person"}`.
  `best` is the top result when its score reaches `min_score`. Pass `uri` (and `user` as `username`)
  to `music_assistant.play_media`.
- `GET /random?type=album&agent=&person=` → `{"item": {...} | null, "user", "person"}`: a random item.
- `agent` / `person` pick whose libraries to use: `person` names the owner (fuzzy, "Aurelii" works),
  otherwise `agent` (the conversation agent that heard the command, e.g. `conversation.alexa`) maps to a
  person through the [person_assistant](https://github.com/ChrisB85/person-assistant) sensors. The option
  `users` maps persons to Music Assistant users. No match = all libraries.
- `POST /aliases/add` `{"artist", "alias", "album"?}` — add one spelling of an artist, or of an album
  when `album` is given (for the voice agent); names may be approximate. Rejects unknown artists and spellings that are another artist's name.
- `GET /api/aliases`, `POST /api/aliases` `{"artist", "aliases": [...]}` — used by the editor.
- `POST /api/stt` (raw 16 kHz mono 16-bit PCM) → `{"text", "transcript"}` — used by the editor.
- `POST /refresh` — reload the library now.
- `GET /health`

## Editor

The app adds a **Zapisy fonetyczne** panel to the sidebar: every artist with their spellings, plus recent
unmatched queries with a one-click "add this spelling to artist". The microphone button next to each
artist records a spelling through Home Assistant's own STT (`stt_entity`, `stt_language`), so it is
exactly what the voice pipeline would write; say it like a command ("Puść Judas Priest") and the
command words are stripped. The browser allows the microphone only over HTTPS; if the sidebar panel
blocks it, open the editor in a new tab (link in the hint).

## Files in `/addon_configs/<prefix>_music_search/`

- `aliases.yaml` — phonetic spellings per artist, as speech-to-text writes them. Edits apply
  immediately.
  ```yaml
  Iron Maiden: [Ajron Mejden]
  ```
- `album_aliases.yaml` — the same for album titles, per artist then album:
  ```yaml
  Black Sabbath:
    Paranoid: [Paranojd]
  ```
- `misses.log` — queries with no confident match; use it to add aliases.
