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
what's currently downloading; `/cancel` drops one that isn't going anywhere.

**Seeders decide whether a download is possible at all**, so ranking weighs
them heavily at the bottom end: zero seeders costs 500 points and gets a
`💀 нет сидов` label, one or two costs 100. A perfectly-labelled Russian
release nobody is sharing is worse than an English one that actually
arrives, and without this it would sit mid-list looking like a sane pick —
which is exactly how the first real download stalled at 0% indefinitely.

`/status` says what a torrent is actually doing rather than repeating
qBittorrent's state name: `💀 нет раздающих` when the swarm is empty (it will
never finish — cancel it), `⏳ ищу пиров` when seeders exist but aren't
connected yet (usually resolves itself).

## Following a show

`/follow <name>` (or the button offered after an import) adds a show to a
once-a-day check. When something turns up, the notification is deliberately
short — just which shows have new episodes and how many:

```
🆕 Вышло новое

• Табакошка — 2
• Youjo Senki II — 1

Посмотреть и скачать — /new
```

`/new` then lists those shows as buttons; picking one re-runs the search and
shows the actual releases to choose from, exactly like `/search`. Nothing is
ever downloaded unattended.

The re-search matters: the daily check stores release *titles*, not links.
Prowlarr signs download URLs per request, so replaying one found hours
earlier can fail — searching again gets a fresh link and current seeder
counts.

The first check records everything currently available without reporting it,
otherwise following a finished show would announce its entire back catalogue.
`/following` lists what's watched, `/unfollow` stops.

The list lives in `telegram-bot/state/following.json`, mounted from the host
so it survives an image rebuild. It's plain JSON and safe to edit by hand.

**Type any name you like.** Before searching, the bot resolves the title
through Shikimori (Russian database) and AniList (romaji/English/native), then
searches every name at once and merges the results. `Yani Neko` also searches
`Табакошка`; `Синий экзорцист` also searches `Ao no Exorcist`.

This matters more than it sounds. Trackers index the same show under
different names — English and anime trackers use romaji, Russian trackers use
the Russian title — so searching one name finds one half of what exists. The
Russian half is the half carrying Russian audio and subtitles: searching
`Yani Neko` alone returned 103 results, none of them Russian, while adding
`Табакошка` surfaced two Russian releases with 52 and 30 seeders.

Duplicates are collapsed by title and size rather than by download URL,
because Prowlarr signs those per request and the same release comes back
under a different URL for each name searched.

## From "downloaded" to "watchable"

qBittorrent saves into `/data/downloads/<kind>/`; Jellyfin only reads
`/data/media/<kind>/`. Sonarr normally bridges that, but it never sees these
downloads — the bot hands torrents straight to qBittorrent, for the reason in
"Why the bot" below.

So the bot bridges it instead (`importer.py`). Once a minute it checks its own
torrents, and for each one that finished:

1. Derives a library title from the release name — release-group handles
   (`- ZaLmanVsk`, `- VARYG`) are stripped, real subtitles (`- Kyoto Saga`,
   `- Final Season`) are kept, and where a Russian release carries both names
   the Latin one wins, because metadata providers match on it.
2. **Hardlinks** each video file into
   `media/<kind>/<Title>/Season NN/<Title> - SNNENN.mkv`. Hardlinks, not
   copies: `downloads/` and `media/` are the same filesystem, so the library
   entry is instant, costs no extra space, and the torrent keeps seeding from
   the original path.
3. Triggers a Jellyfin library refresh and posts "готово" to the topic.

Episode numbers are read from `S01E05`, `- 05 -`, `05 of 12`, or a leading
`05.` — the four shapes these indexers actually produce. A file with no
recognisable number is still linked, just unrenamed, rather than dropped.

Re-running is safe: files already present are skipped, so nothing is
duplicated if the job runs again over the same torrent.

## Managing what's on the server

`/library` lists every show with its file count and size, grouped by kind.
`/disk` shows free space and how much the seeding torrents account for.

`/delete` removes a show properly: the library files **and** the torrent it
came from. Both matter — the library files are hardlinks to the download, so
deleting only one side frees nothing. It takes a second confirming press,
being the one irreversible action here.

Deleting a show in Jellyfin instead also removes the library files (if the
library permits it) but leaves the torrent seeding, so the space stays used.
Prefer `/delete`.

## The control panel

The bot attaches a row of buttons under its own messages — Status, New,
Following, Library, Disk, Help — covering everything that takes no argument.
Searching still needs typing, since a button can't carry a title.

These are **inline** buttons, not a reply keyboard, and that choice is forced
by where the bot lives. A reply keyboard is the obvious fit and does pin
itself under the message box — but only in a private chat. In a group
Telegram collapses it into a small icon beside the input field, so in a forum
topic it isn't a visible panel at all. Inline buttons render the same
everywhere and stay clickable in the history.

The `/` menu beside the message box lists every command with a description;
Telegram keeps it in sync, which a pinned message wouldn't.

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

**Sonarr and Radarr are not in this path at all.** They were tried as a way
to reach Bazarr — which builds its library from Sonarr rather than from disk
— but the trade was bad: Sonarr can only search the English title, so it
finds English releases and misses the Russian ones that carry the audio and
subtitles actually wanted. It also renames what it adopts, and once deleted
a folder of imported files while trying to; `renameEpisodes` is off for that
reason. They stay running but do nothing the bot depends on.

New episodes of a show are handled by `/follow` instead, which reuses the
bot's own multi-title search and so finds Russian releases the same way the
first download did.

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

**Russian anime subtitles mostly do not come from subtitle providers.** They
ship with the release — either muxed into the `.mkv`, or as separate `.ass`
files beside it — and the reliable way to get them is to download a release
that already has them, which is what multi-title search above is for. The
importer links those sidecar files in as `<Episode>.ru.ass`, the naming
Jellyfin reads as a selectable Russian track.

Language is decided by sampling the `Dialogue:` lines rather than the head of
the file: an `.ass` can open with megabytes of styling before the first line
of speech, and in one real release the first Cyrillic character sat at byte
2.8M of 2.9M. Reading a prefix labelled a Russian subtitle as English. Checked on real files: a Russian release of one show
carried `rus (Надписи)`, `rus (Полные)`, `eng`, four Russian dubs and the
Japanese track; the English release of another carried no Russian at all,
and no provider had any either.

Sources that sound like they should help, and don't:

| Source | Reality |
|---|---|
| Kitsunekko | Japanese, Chinese and Korean only — there is no Russian section |
| AniLibria | dubs, not subtitles, and a narrow catalogue |
| fansubs.ru | times out from this host |
| AnimeLib and similar streaming sites | no open API; subtitles are served to their player, not downloadable |

So Bazarr is worth having for live-action and for English subtitles, but for
anime the release choice does the work.

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
- **`/torrents/add` answers `Ok.` even when the torrent is dropped**, so the
  bot polls for the new infohash instead of trusting the response. The usual
  cause is a duplicate: multi-title search surfaces the same release under
  both its English and Russian names, and picking the other one looks like a
  fresh download but silently does nothing. That now reports "уже скачано"
  rather than a queued download that never starts.
- **Deleting a series in Jellyfin deletes the files**, if the library allows
  it — the hardlinks in `media/` go, but the originals in `downloads/` stay,
  so re-importing from the torrent restores everything without downloading
  again.
- **`/mnt/storage` is mounted whole** into Sonarr, Radarr, Bazarr and
  qBittorrent as `/data`, so hardlinks work between `downloads/` and `media/`.
  Mounting the two separately would make every import a full copy.

See [../drive/docs/architecture.md](../drive/docs/architecture.md) for how the
rest of the Citadel fits together.
