# ownCloud

File storage with sync and sharing. Version is pinned in `.env`.

## Containers

| Name | Role |
|---|---|
| `owncloud_server` | PHP app, WebDAV |
| `owncloud_mariadb` | users, permissions, file index |
| `owncloud_redis` | metadata cache and locking |

## Data

```
/mnt/hdd/owncloud/files/<user>/files/   user files
/mnt/hdd/owncloud/backups/              nightly database dumps, 02:30
./mysql/                                database files, on NVMe
./redis/
```

Only `files/<user>/files/` and `backups/` go into the backup. Trash, cache,
and incomplete uploads are excluded on purpose.

## Running it

```bash
cd ~/Citadel/drive/owncloud && docker compose up -d
```

```bash
docker compose logs -f owncloud_server
```

## Managing users

All `occ` commands run as the web-server user:

```bash
docker exec -u www-data owncloud_server occ user:list
```

Create a user (password prompted interactively, `-it` is required):

```bash
docker exec -it -u www-data owncloud_server occ user:add --display-name="Name" login
```

Reset a password:

```bash
docker exec -it -u www-data owncloud_server occ user:resetpassword login
```

Issue a link for the user to set their own password — handy when even the
admin shouldn't know it:

```bash
docker exec -it -u www-data owncloud_server occ user:resetpassword --output-link login
```

An admin can also set a password through the UI: **Settings → Users**, a
pencil icon appears in the Password column on hover.

## Important: configuration is set via environment variables

The image's entrypoint **rebuilds `config.php` on every start** from
environment variables. Edits made through `occ config:system:set` survive
while the container keeps running, but vanish on the next start.

So everything that matters is set in `docker-compose.yml`. To see which
variables are supported:

```bash
docker exec owncloud_server grep -n "getenv" /etc/templates/config.php
```

A value that's already made it into `config.php` doesn't disappear on its
own just because you remove the variable — delete it explicitly:

```bash
docker exec -u www-data owncloud_server occ config:system:delete key
```

## Running behind a reverse proxy

TLS terminates upstream, hence:

- `OWNCLOUD_OVERWRITE_PROTOCOL=https` — otherwise ownCloud builds `http://`
  links and clients break on redirects
- `OWNCLOUD_TRUSTED_PROXIES` — the docker network's gateway address
- `OWNCLOUD_TRUSTED_DOMAINS` — every name the service is reachable under

`overwritehost` is deliberately **left unset**: the service answers on both
the domain and the Tailscale address, and pinning it to one name would break
the other.

The docker network's subnet is pinned in compose so the gateway address in
`TRUSTED_PROXIES` doesn't shift when the network gets recreated.

## Files dropped in from outside ownCloud

It won't see them until indexed:

```bash
docker exec -u www-data owncloud_server occ files:scan --path=/login/files
```

The Google Drive sync script does this automatically.

More detail: [../docs/architecture.md](../docs/architecture.md)
