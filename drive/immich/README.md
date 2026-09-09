# Immich

Photo and video library with face recognition and text search. Version is
pinned in `.env`.

## Containers

| Name | Role |
|---|---|
| `immich_server` | API, web UI, upload handling |
| `immich_machine_learning` | face recognition, text search (CLIP) |
| `immich_postgres` | database with the vector-search extension |
| `immich_redis` | background job queue (Valkey) |

## Data

```
/mnt/hdd/immich/upload/<uuid>/    originals — the only irreplaceable part
/mnt/hdd/immich/thumbs/           thumbnails, regenerated on demand
/mnt/hdd/immich/encoded-video/    transcodes, regenerated on demand
/mnt/hdd/immich/backups/          nightly database dumps, 02:00
./postgres/                       database files, on NVMe
```

Only `upload/`, `profile/`, and `backups/` go into the backup. Immich
regenerates thumbnails and transcodes on its own — no point paying to store
them.

## Running it

```bash
cd ~/Citadel/drive/immich && docker compose up -d
```

```bash
docker compose logs -f immich_server
```

## First login

Whoever registers first becomes the administrator.

## Common tasks

Background job status — **Administration → Jobs**. During a bulk import
these load every core for hours; that's expected.

Reset the admin password:

```bash
docker exec -it immich_server immich-admin reset-admin-password
```

List users and their UUIDs (needed to set up backups):

```bash
docker exec immich_postgres psql -U postgres -d immich -t -c 'select id, name, email from "user";'
```

## Upgrading

1. Check the release notes for database migrations
2. Confirm last night's backup succeeded
3. Bump `IMMICH_VERSION` in `.env`
4. `docker compose pull && docker compose up -d`

Rolling back after a migration has applied requires restoring the database
from a dump — migrations are usually one-way.

## Implementation notes

The Postgres image is pinned by SHA digest: it's a custom build with the
vector-search extension, plain `postgres:14` won't work.

The port listens on `127.0.0.1` and the Tailscale address. Not exposed to
the LAN.

More detail: [../docs/architecture.md](../docs/architecture.md)
