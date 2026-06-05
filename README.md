# jarvis-cmd-audacy

Voice command for streaming live radio from the **Audacy** network on a
Jarvis node. Sports talk (WFAN, 670 The Score, 94 WIP), news (1010 WINS,
WBBM, KCBS), rock (KROQ, Q104.3), and more — no login required, no Music
Assistant required. Direct stream playback through a local `mpd` daemon.

## Voice surface

| Say | Action |
|---|---|
| "Play WFAN radio" | Tune to WFAN |
| "Put on 670 The Score" | Tune to WSCR |
| "Tune in to KROQ" | Tune to KROQ |
| "Play 1010 WINS" | Tune to WINS |
| "Play the FAN" | Tune to WFAN (nickname) |
| "Play Audacy" | Resume last station (or nudge user) |
| "What station is this?" | Spoken now-playing |
| "Stop the radio" / "Stop Audacy" | Stop playback |
| "What Audacy stations do you have?" | Sample of the catalog |

## How playback works

```
You: "Play WFAN radio"
    │
    ▼
Jarvis command-center  →  audacy command  →  mpd (local daemon)
                                                     │
                                     HTTP/HLS stream │
                                                     ▼
                                            PulseAudio / PipeWire
                                                     │
                                                     ▼
                                     Local speaker  -or-  paired BT speaker
```

This package never spawns a subprocess. All playback runs through `mpd`'s
TCP control protocol (`localhost:6600`). On install, the node:

1. Installs `mpd` via the Pantry's sudoers-gated `jarvis-apt-install` helper.
2. Copies the package's `audacy_shared/audacy_mpd.conf` into
   `~/.jarvis/packages/audacy/audacy_lib/audacy_shared/` — runs as the
   node user, no privilege escalation needed.
3. Re-parents `mpd.service` to run as the `jarvis_user` and sets
   `Environment=MPDCONF=…audacy_mpd.conf` so mpd reads the per-package
   config instead of `/etc/mpd.conf`. The system `/etc/mpd.conf` is left
   completely untouched (avoiding the `user "mpd"` directive and
   mpd-user-owned `/var/lib/mpd` dirs that would otherwise need root).

## Architecture

```
jarvis-cmd-audacy/
├── jarvis_package.yaml          # manifest (apt + post_install + components)
├── commands/audacy/command.py   # IJarvisCommand subclass
├── audacy_shared/
│   ├── stations.yaml            # catalog — edit to add stations
│   ├── station_catalog.py       # loader + fuzzy resolver
│   ├── stream_resolver.py       # AmperWave + StreamTheWorld URL builder
│   ├── mpd_client.py            # raw TCP client (~140 lines, stdlib only)
│   ├── audacy_service.py        # singleton orchestrator
│   └── audacy_mpd.conf          # per-package mpd config (read via MPDCONF)
└── README.md
```

## Pre-route claim policy

Four tiers, narrowest-correct claim wins:

1. **Explicit "audacy"** — "play X on audacy" / "play audacy" / "stop audacy" — always claim.
2. **Radio-suffix structure** — "play X radio", "tune in to X", "put on X station" — claim if X resolves in the catalog. The catalog gate prevents stealing "play classical radio" (which means a genre, not a station) from Spotify.
3. **Bare "play X"** — "play WFAN", "put on the score" — claim if X resolves in the catalog. The catalog's fuzzy matcher (token overlap ≥ 0.6, trivial words stripped) is conservative enough that song / artist queries don't accidentally hit.
4. **Bare stop/pause** — claim only when Audacy is the active player (mirrors Pandora's gate pattern).

## Adding stations

Stations live in [`audacy_shared/stations.yaml`](./audacy_shared/stations.yaml).
Each entry needs a callsign, band (`am` / `fm`), display name, and a few
aliases the user might say. The stream URL is constructed automatically
from AmperWave's conventional slug; if a station doesn't follow the
convention, add `stream_url:` with an explicit URL.

```yaml
- callsign: WBT
  band: am
  display_name: WBT News Talk Sports
  aliases: ["wbt", "wbt charlotte", "1110 wbt"]
  format: talk
  market: Charlotte
  # Optional explicit override:
  # stream_url: https://stream.example.com/wbt.m3u8
```

## Dev setup

```bash
# Install the package's deps into the toolkit venv
/path/to/jarvis-developer-toolkit/.venv/bin/pip install pyyaml

# Run validation (manifest + AST + import checks)
/path/to/jarvis-developer-toolkit/.venv/bin/jdt test .

# Run mpd locally
# macOS:  brew install mpd && brew services start mpd
# Linux:  sudo apt install mpd && sudo systemctl start mpd
```

## Pantry allow-list updates (needed before publishing)

Audacy needs two small additions to `jarvis-pantry/config/`:

### `apt-allowlist.yaml`

```yaml
- name: mpd
  reason: "Music Player Daemon — direct-stream playback backend for the audacy command. Lightweight (~10MB resident), suitable for Pi Zero."
  added_by: alex
  added_at: "2026-06-05"
```

### `post-install-allowlist.yaml` — `configure_systemd_service`

```yaml
- service: mpd
  reason: "Per-node config for mpd — User=jarvis_user + pulse env + MPDCONF pointing at the package's audacy_shared/audacy_mpd.conf so we never touch /etc/mpd.conf (which has user \"mpd\" + /var/lib/mpd ownership that would otherwise need root)."
  added_by: alex
  added_at: "2026-06-05"
```

No `set_config_file_value` entry is needed — the package ships its own
mpd config inside `audacy_shared/` and points mpd at it via `MPDCONF` in
the systemd dropin's `Environment=`. This sidesteps the entire question
of how to safely mutate `/etc/mpd.conf` from a sudoers-gated wrapper.

## License

MIT — see [LICENSE](./LICENSE).
