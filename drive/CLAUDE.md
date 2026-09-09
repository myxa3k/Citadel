# Context for an AI assistant

This file exists so a new session can pick up work on this server without a
full history dump. Read it in full, then [ENVIRONMENT.md](ENVIRONMENT.md)
(current IP/domain/VM numbers), and [docs/architecture.md](docs/architecture.md)
and [docs/backups.md](docs/backups.md) as needed.

Asked to add a new service alongside Immich/ownCloud? Read
[CONTRIBUTING.md](CONTRIBUTING.md) first — it's the pattern already in use,
not a style guide to reinvent.

---

## What this is

A home server: **Immich** (photos) and **ownCloud** (files) in Docker on a
Debian 13 VM under Proxmox. Access only over the private Tailscale network.
Three backup layers: ZFS mirror, VM snapshots, restic encrypted to Backblaze
B2.

The project's goal is moving off paid Google Photos / Google Drive
subscriptions onto owned hardware.

Two users: the owner and a friend. Data is separated at the filesystem
level; each can have their own backup repository and their own cloud.

---

## Connecting

The current address is in [ENVIRONMENT.md](ENVIRONMENT.md), not here: the
tailnet IP and hostname change when the VM gets rebuilt, and repeating them
in prose is guaranteed to go stale.

```
ssh shiro@<tailscale-ip-or-hostname-from-ENVIRONMENT.md>
```

User `shiro`, in the `sudo` group. You need to be on the same tailnet.

**This stack's VM lives on a Proxmox host** (number and hostname in
ENVIRONMENT.md). The assistant usually has no access to that host — Proxmox
commands get handed to the owner to run by hand.

---

## Layout

```
~/Citadel/drive/            this repo, also the working directory for the stacks
├── immich/                 compose + .env + postgres/ (database data)
├── owncloud/                compose + .env + mysql/ + redis/
├── docs/                     documentation
├── scripts/                  maintenance scripts (copies of /usr/local/bin)
└── systemd/                  units (copies of /etc/systemd/system)

/mnt/hdd/immich/             media library — see ENVIRONMENT.md for current size
/mnt/hdd/owncloud/           user files — see ENVIRONMENT.md for current size
/etc/restic/                 backup settings, root:root 600
/root/.config/rclone/        Google Drive access, root:root 600
/usr/local/bin/              the scripts actually running
```

Databases live on NVMe (`~/Citadel/drive/*/`), media on the HDD mirror
(`/mnt/hdd/*`). Deliberate split, not an accident.

---

## Addresses

| Service | Primary | Fallback |
|---|---|---|
| Immich | `https://immich.citadel-home.xyz` | `https://<tailscale-hostname>` |
| ownCloud | `https://owncloud.citadel-home.xyz` | `https://<tailscale-hostname>:8443` |

`<tailscale-hostname>` — see ENVIRONMENT.md, changes on a VM rebuild. The
`citadel-home.xyz` domain is stable, no plans to change it.

Primary goes through Nginx Proxy Manager on a separate LXC. Fallback goes
through `tailscale serve` directly from this machine and doesn't depend on
the proxy.

---

## Schedule

```
01:00  gdrive-sync              Google Drive → ownCloud   (only if that timer is enabled)
02:00  (built into Immich)      Postgres dump
02:30  owncloud-db-backup       MariaDB dump
03:00  restic-backup@personal   everything → Backblaze B2
04:00  vzdump                   VM snapshot (on the Proxmox host)
05:00  restic-check@personal    integrity check, Sundays
hourly  health-watch            monitoring
```

Order matters: the 03:00 backup needs to catch fresh dumps.

---

## Rules for working on this system

**Change configuration through files, not commands.** ownCloud's settings
are set via environment variables in `docker-compose.yml`: the image's
entrypoint rebuilds `config.php` on every start and wipes out `occ` edits.
Check whether a variable is supported:

```bash
docker exec owncloud_server grep -n "getenv" /etc/templates/config.php
```

**Versions are pinned.** No `latest` tags. An upgrade is a deliberate,
separate action with release notes checked and a fresh backup in hand.

**Ports are bound to specific addresses** (`127.0.0.1` and the Tailscale
address), never `0.0.0.0`. Don't change that — it would expose the services
to the LAN.

**Secrets stay out of the repository.** `.gitignore` already lists `.env`,
`rclone.conf`, `*.conf` under `/etc/restic`, and keys. Only `*.example`
files belong in the repo.

**Verify the result, not just that a job ran.** Data was already lost here
once to a backup that ran every night and never left a single restorable
archive. Once you make a backup, restore a file from it and check the
checksum.

---

## Things not to do

- Put live database directories (`stack/*/postgres`, `stack/*/mysql`) into
  the backup — a live copy is torn and unrestorable. That's what dumps are
  for.
- Set `overwritehost` in ownCloud — the service answers on two names, and a
  hard pin would break one of them.
- Turn on Object Lock on the B2 bucket — it stops restic from deleting old
  blocks, and rotation will start failing.
- Disable SSH password login until the owner's key has actually shown up in
  `authorized_keys`. Otherwise the only access left is the Proxmox console.
- Flip Google Drive sync from `copy` back to `sync`. It's `copy` right now:
  local files never get deleted, even after cleaning up the Drive.

---

## Where things are locked down

Locations only, so real maintenance work doesn't need a file hunt — the
values themselves live in the files, not here.

| What | Where | Permissions |
|---|---|---|
| Immich database passwords | `~/Citadel/drive/immich/.env` | 600 shiro |
| ownCloud database and admin passwords | `~/Citadel/drive/owncloud/.env` | 600 shiro |
| B2 keys and the repository password | `/etc/restic/personal.env` | 600 root |
| Google Drive token | `/root/.config/rclone/rclone.conf` | 600 root |
| Assistant's SSH key | `~/.ssh/authorized_keys` | 600 shiro |

**The restic repository password is unrecoverable** — keep a copy off this
server. Everything else can be recreated.

---

## Quick diagnostics

```bash
/usr/local/bin/health-watch.sh --verbose
```

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

```bash
systemctl list-timers --no-pager | grep -E "restic|gdrive|owncloud-db|health"
```

Symptoms and fixes — [docs/troubleshooting.md](docs/troubleshooting.md).
Same file has the list of pitfalls already hit once.

---

## Current state

Don't trust memory, or past entries in this section — **verify live**, the
commands below are for exactly that. Past findings and migrations are
deliberately not narrated here: that kind of note goes stale faster than
anyone re-reads it, and risks someone treating an old fact as a current one.

Check what's actually working:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
systemctl list-timers --no-pager | grep -E "restic|gdrive|owncloud-db|health"
systemctl is-enabled gdrive-sync.timer   # is Google Drive sync turned on
sshd -T | grep passwordauthentication    # is password login still allowed
```

The full restore-on-a-new-machine procedure — with the pitfalls found in
practice — is in [docs/backups.md](docs/backups.md), under "Full restore
on a new machine." It already accounts for things like the restoring user's
UID, the partition size Docker needs, and the order of stopping the service
before loading a dump — read it in full before restoring rather than relying
on a summary here.

**Known open items** (verify with the commands above — this list may not
get updated promptly):

- No account or separate backup repository set up yet for the friend
- SSH password login may still be enabled — only turn it off once the
  owner's key is confirmed in `authorized_keys`
- SMTP isn't configured: password-reset emails and notifications don't go out
- On the Proxmox host, worth checking whether thick-provisioned zvols from
  unused VMs are wasting space (`zfs get refreservation`) — hasn't been
  checked in a while
