# Adding a service to drive

This isn't a style guide written in the abstract — it's the pattern already
followed by `immich/` and `owncloud/`, written down so a third service (or a
fourth) follows the same shape instead of drifting into its own thing. If
you're about to add a service and this file and reality disagree, reality
wins — fix this file.

If what you're adding isn't Immich/ownCloud-specific — a Proxmox-host script,
a check for some other VM — it probably doesn't belong under `drive/` at
all. See the top-level repo layout: each infra-wide concern (alerting is the
current example, in `telegram/`) gets its own top-level directory, not a
subfolder of `drive/`.

---

## The shape of one service

Every service under `drive/` is a folder holding:

```
<service>/
├── docker-compose.yml
├── .env              (real values, gitignored, never committed)
├── .env.example       (placeholders, committed)
└── README.md
```

### `docker-compose.yml`

- `name: <service>` at the top — pins the Compose project name to the
  service, independent of what directory it happens to sit in.
- Container names are `<service>_<role>` (`immich_server`,
  `owncloud_mariadb`). **This isn't just a naming preference** —
  `health-watch.sh` routes container-health findings to a Telegram category
  by matching this exact prefix (see below). A container named anything else
  falls into the generic `system` category instead of its own.
- Ports bind to `127.0.0.1` and `${TAILSCALE_IP}` explicitly, never
  `0.0.0.0`. `TAILSCALE_IP` comes from `.env`, sourced from
  [ENVIRONMENT.md](ENVIRONMENT.md) — never a literal IP in the compose file
  itself (see "the found the hard way" list below for why).
- Pin versions. No `latest` — see `CLAUDE.md`'s rules for why.
- A comment block at the top explaining the two or three decisions that
  aren't obvious from the YAML alone (why this volume, why this restart
  policy) — see either existing service's compose file for the tone.

### `.env` / `.env.example`

`.env.example` has every key the real `.env` needs, values replaced with
something like `REPLACE_WITH_RANDOM_PASSWORD` or a placeholder describing
what goes there. `.gitignore` already excludes `.env` and allows
`.env.example` — don't touch that pattern per-service, it's global.

### `README.md`

Match the structure already used by `immich/README.md` and
`owncloud/README.md`:

```
# <Service>
one-line description, version-pinning note

## Containers
table: name → role

## Data
what's under /mnt/hdd/<service>/, what's excluded from backup and why

## Running it
cd + docker compose up -d

## Common tasks
whatever operators actually need day to day

## Upgrading
the steps, and what to check first

## Implementation notes
anything non-obvious about how it's wired up

link to ../docs/architecture.md at the end
```

---

## Data and backups

- Bulk data (photos, files, anything measured in gigabytes) goes on the HDD
  mirror: `/mnt/hdd/<service>/`. Databases stay on NVMe, under
  `<service>/<db-engine>/` inside the service's own folder — same split
  Immich and ownCloud already use, see
  [docs/architecture.md](docs/architecture.md) for why.
- A live database directory (`postgres/`, `mysql/`, whatever) needs **two**
  separate exclusions, not one:
  1. Added to `.gitignore` (so git never tracks it — it's gigabytes and
     changes constantly).
  2. Added to `restic-backup.sh` — and this one is **manual code editing, not
     a config list**. `PATHS` and `EXCLUDES` in that script are hardcoded
     bash arrays built with explicit `add "..."` calls and literal
     `--exclude` entries per known service (see the file itself for the
     current shape). A new service needs a new `add` call for its actual
     data path, and a new `--exclude` line in `EXCLUDES` for its live
     database directory — there's no loop over `drive/*/` that picks this up
     automatically. Skip either one and the new service's data either never
     gets backed up, or its torn live database copy does.
- A new service with its own database needs its own dump job, on the same
  pattern as `owncloud-db-backup.sh`: dump to a temp file, rename only on
  success, keep the last N, wire a systemd timer for it, and make sure it
  runs *before* `restic-backup@personal.service` at 03:00 so the backup
  picks up a fresh dump rather than yesterday's.
- If the new service holds user data that needs its own backup profile
  (separate encryption key, separate cloud account) — see
  [docs/backups.md](docs/backups.md) "How profiles work." It's a new
  `.env`/`.conf` pair under `/etc/restic/`, the script and timer stay
  shared.

---

## Wiring into monitoring and alerts

`drive/scripts/health-watch.sh` is what watches this service, and it's
**drive's own file** — a new service means editing it directly, not calling
some generic hook. Concretely, that means:

- A disk-pressure check for a new mount point: add it to the `for mp in ...`
  loop, with a `case` entry routing it to a category via `add_<category>`.
- A container-health check: this one's automatic *if* the container names
  follow the `<service>_<role>` convention above — the existing `case`
  statement on the prefix just needs a new arm.
- A staleness check for a new scheduled job: copy the pattern already used
  for `restic` and `gdrive-sync` — a `.last-success` timestamp file the job
  writes on success, and a threshold here that flags it stale.

Whether that new category needs its own Telegram topic, or can fall into
`General`/`Drive`/`Backups`, and how to wire up the routing itself — that's
`telegram/`'s concern, not drive's. See
[`telegram/README.md`](../telegram/README.md), "Adding a new category."
`drive/docs/notifications.md` only documents which category *this repo's*
checks use, nothing about the delivery mechanism.

---

## Documentation

- Anything specific to *this instance* (an IP, a domain, a VM number, a
  bucket name) goes in [ENVIRONMENT.md](ENVIRONMENT.md), referenced by
  placeholder everywhere else — never repeated as a literal fact in prose.
  See the top of that file for why: it's the one thing that goes stale on a
  rebuild, and the one file meant to absorb that.
- Don't narrate history — no "on this date we found X," no snapshot counts,
  no incident timelines. A lesson learned belongs as a generic rule (in this
  file, in `docs/troubleshooting.md`'s pitfalls table, or inline in whatever
  procedure it affects), stripped of the specific numbers and dates that
  made it true only once. See `docs/backups.md`'s restore procedure for what
  that looks like done well — every finding from an actual restore drill is
  in there as a numbered, actionable step, with zero dates or counts.
- Everything committed to this repo is English. Chat with the user can be
  any language; nothing that ends up in a file here is.

---

## Found the hard way — apply to any new service too

These aren't drive-specific bugs, they're patterns that will bite a new
service exactly the same way if repeated:

- **A literal IP or tailnet hostname baked into a compose file or a doc**
  goes stale the moment the VM gets rebuilt, silently, with no signal that
  it happened. Always a variable from `.env`, sourced from
  `ENVIRONMENT.md` — never typed twice.
- **A container's own entrypoint rebuilding its config from environment
  variables on every start** (ownCloud does this) means edits made through
  the app's own admin CLI vanish on the next restart. If a new service's
  image does the same thing, the settings belong in `docker-compose.yml`'s
  `environment:` block, not in a runbook step that runs some in-container
  command once and hopes it sticks.
- **A database dump landing under a live application** produces a dump that
  looks fine and doesn't restore. Any restore procedure for a service with
  its own database stops the app (not the database container) first.
- **Restoring a config value with `restic restore`** brings back whatever
  was true on the *old* machine, including its old tailnet address. A
  freshly restored service needs that value re-pointed at the new machine's
  `ENVIRONMENT.md`, the same as the original setup did.
