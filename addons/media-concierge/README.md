# Optional Media Concierge

Media intents in the Alexa interaction model use this local addon. It checks
Plex, requests missing movies through Radarr or selected TV seasons through
Sonarr, and tracks download/import/Plex readiness. Movie lookup does not request
a download unless the spoken request explicitly asks for one. TV acquisition
requires a season selection. Existing profiles and global NZBGet pause remain
unchanged.

Create a private `~/.local/share/media-concierge/config.json`, with directory
mode 700 and file mode 600. Populate it with your own values; a schema example:

```json
{
  "plex": {
    "url": "",
    "token": "",
    "server_id": "",
    "section_ids": {"movie": [], "series": []}
  },
  "radarr": {
    "url": "",
    "api_key": "",
    "quality_profile_id": 0,
    "root_folder": ""
  },
  "sonarr": {
    "url": "",
    "api_key": "",
    "quality_profile_id": 0,
    "root_folder": ""
  },
  "nzbget": {"url": "", "username": "", "password": ""},
  "path_maps": {"radarr": {}, "sonarr": {}}
}
```

Use string Plex section IDs and real numeric quality-profile IDs. URLs should
be reachable only through your trusted network. Path maps translate each
Radarr/Sonarr container media-root prefix into Plex's corresponding host path
for targeted refreshes. No household-specific paths are included here.

For a read-only library check:

```sh
.venv/bin/python addons/media-concierge/concierge.py check "Example Movie" --kind movie
```

For durable progress, schedule the following locally every minute under the
same OS account as the bridge:

```sh
.venv/bin/python addons/media-concierge/concierge.py monitor
```

The addon records requests in its private SQLite database and uses a mutation
lock. It bounds repeated Plex scans and reports ambiguous titles or uncertain
writes honestly. It does not install or configure Plex, Radarr, Sonarr or NZBGet.
Use only media acquisition sources you are authorized to use. Never commit the
filled configuration or database.
