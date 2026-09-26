# Streaming

Media library and the machinery that fills it: Jellyfin to watch, a Telegram
bot to search and download, and the *arr stack for everything automatic.
All versions are pinned in `streaming-compose.yaml`.

Runs on its own VM, separate from `drive/`. Ports bind to `127.0.0.1` and the
tailnet address only — nothing is exposed to the LAN except the BitTorrent
peer port, which has to be reachable for incoming connections.

## Containers

| Name | Role |
|---|---|
| `streaming_jellyfin` | media server and player |
| `streaming_bot` | Telegram bot — search, pick a release, start a download |
| `streaming_prowlarr` | indexer manager; every search goes through it |
| `streaming_flaresolverr` | solves Cloudflare challenges for indexers that need it |
| `streaming_sonarr` | TV automation (new episodes of tracked shows) |
| `streaming_radarr` | film automation |
| `streaming_bazarr` | fetches subtitles for what the others import |
| `streaming_qbittorrent` | torrent client |
| `streaming_seerr` | request/discovery UI |
| `streaming_filebrowser` | manual uploads from a phone or PC |

## Data

```
/mnt/storage/media/anime|series|movies/   the library Jellyfin serves
/mnt/storage/downloads/anime|series|movies/  in-progress and seeding torrents
/mnt/storage/manual-import/               filebrowser drop zone
./<service>/config/                       per-service state, on the system disk
```

Media sits on the 500 GB data disk; service config stays on the system disk.
**None of this is backed up** — it's all re-downloadable, and the volume
(hundreds of gigabytes of torrents) would make an off-site backup pointless.
That's a deliberate difference from `drive/`, where the data is irreplaceable.

## Running it

```bash
cd ~/docker/streaming && docker compose -f streaming-compose.yaml up -d
```

```bash
docker compose -f streaming-compose.yaml logs -f streaming_bot
```

## Downloading something

In the Citadel group's **Streaming** topic:

```
/search Синий экзорцист
```

The bot searches every indexer at once, ranks what comes back (Russian audio
and subtitles first, non-video junk last), and shows it as buttons. Pick a
release, pick where it goes, and it's handed to qBittorrent. `/status` shows
what's currently downloading.

**Search by the name the tracker uses.** Russian trackers list
«Синий экзорцист», not "Blue Exorcist" — the bot passes the query through
untouched, so either works depending on where the release lives. This is the
whole reason the bot exists: see "Why the bot" below.

## Manual uploads

`http://<tailscale-ip>:8081` — drag a file in, it lands in
`/mnt/storage/manual-import/`. Useful when a release exists nowhere the
indexers can see it, or it's already downloaded on a laptop.

Nothing about this path touches ownCloud on the drive VM: no accounts to
create per person, and nothing enters the restic backup.

## Why the bot

Sonarr and Radarr search using the title their metadata provider (TheTVDB /
TMDB) gives them, which is the English or romanised one. Russian trackers
index the Russian title. The gap is not small — for one show, searching RuTor:

```
"Blue Exorcist"      1 result
"Синий экзорцист"   19 results
```

No amount of quality-profile or release-profile tuning fixes that; the
releases were never in the result set to begin with. The bot sidesteps it by
querying Prowlarr directly with whatever you typed, and by letting you choose
the release rather than inferring which one you meant.

Sonarr and Radarr still earn their place: they watch tracked shows for new
episodes, rename and organise files into the library, and hand subtitles to
Bazarr. They just aren't the way a *specific* thing gets downloaded.

## Indexers

Configured in Prowlarr, currently: NoNaMe Club, RuTor, MegaPeer (Russian),
Anime Tosho and The Pirate Bay (English). MegaPeer goes through FlareSolverr.

Three indexers were removed rather than repaired, and it's worth knowing why
before adding them back:

- **RuTracker** — its login page answers `403` to anything that isn't a real
  browser, and FlareSolverr times out on the challenge from a datacentre IP.
  Reachable only from a residential address.
- **Nyaa.si** — `504` from this host, consistently. The site's own problem.
- **showRSS** — DNS/SSL failure on every request.

Check indexer health after any change:

```bash
curl -s -H "X-Api-Key: <key>" http://127.0.0.1:9696/api/v1/indexerstats | jq
```

A non-zero `numberOfFailedQueries` against a low `numberOfQueries` means that
indexer is dead weight — it slows every search by its timeout and contributes
nothing.

## Subtitles

Bazarr pulls the library from Sonarr and Radarr and fetches subtitles for it.
The language profile is **Russian first, English second**, applied by default
to everything new.

Provider choice is constrained by the same thing that killed RuTracker: this
host's IP is a datacentre address, and most subtitle sites treat it
accordingly. What was tried and why it was dropped:

| Provider | Outcome |
|---|---|
| `opensubtitlescom` | the one that works — needs a free account, filled into `opensubtitlescom.username`/`password` |
| `subtitlecat` | reachable, no account, but rarely has anything |
| `subf2m` | needs a `user_agent` set, and then its search endpoint returns 500 |
| `subsource` | requires an API key |
| `animetosho` | requires AniDB credentials |
| `podnapisi` | not shipped in this Bazarr build at all |
| `opensubtitles.com` (web), `subdl` | 403 to this IP |

A provider that errors gets throttled for 10 minutes to 12 hours and every
search waits on it first, so a broken one is worse than an absent one — keep
`enabled_providers` to what actually works. Throttle state lives in
`config/config/throttled_providers.dat`; deleting its contents (`echo '{}' >`)
clears it without waiting the timer out.

## Language preferences

Sonarr and Radarr score releases with Custom Formats, set up to prefer
Russian: `RU Dub (D/P/MVO/DVO)` +200, `Cyrillic title` +150, and
`Non-video junk (games/mobile)` −10000 so game repacks can never win.

The patterns come from real release titles on the configured indexers. Two
inherited formats were zeroed out because they did the opposite of what their
names suggested:

- `rus subs` scored 500 but its pattern matched no Russian release actually
  returned by these indexers — it was written for English-language tags.
- `Japanese audio` scored 100 on the word `dual`, so English dual-audio
  releases collected a bonus meant for Japanese ones.

The bot does its own ranking (`ranking.py`) and doesn't consult these — they
only affect what Sonarr/Radarr pick on their own.

## Upgrading

1. Read the release notes for whatever is moving
2. Bump the tag in `streaming-compose.yaml`
3. `docker compose -f streaming-compose.yaml up -d <service>`
4. Check it came back: `docker compose ps`, then the service's own UI

Rolling back is putting the old tag back and running the same command. Service
config lives in `./<service>/config/` and survives a container being replaced.

## Implementation notes

- **Container names are `streaming_<role>`** and the apps address each other
  by those names over the compose network. Renaming a container breaks
  Prowlarr's app connections until they're updated — they were pointing at the
  pre-rename `sonarr:8989` and silently failing to sync.
- **`net.ipv4.ip_nonlocal_bind=1`** plus a `docker.service.d` drop-in ordering
  Docker after `tailscaled`. Without both, containers binding to the tailnet
  address fail with `cannot assign requested address` after a reboot — the
  same pitfall documented in `../drive/docs/troubleshooting.md`.
- **qBittorrent's WebUI answers `204` to a failed login**, not just a
  successful one. Only the presence of the `QBT_SID` cookie proves the
  password was right; the bot's client checks for exactly that.
- **`/mnt/storage` is mounted whole** into Sonarr, Radarr, Bazarr and
  qBittorrent as `/data`, so hardlinks work between `downloads/` and `media/`.
  Mounting the two separately would make every import a full copy.

See [../drive/docs/architecture.md](../drive/docs/architecture.md) for how the
rest of the Citadel fits together.
