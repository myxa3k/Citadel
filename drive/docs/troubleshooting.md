# When something breaks

Start with general diagnostics, then jump to the specific symptom. Almost
every entry below comes from a real failure hit while setting this system
up, not from general theorizing.

---

## Always start here

```bash
/usr/local/bin/health-watch.sh --verbose
```

One command covers most cases: disk space, container health, backup
freshness, unprotected accounts, whether a reboot is needed.

Then:

```bash
docker ps -a --format "table {{.Names}}\t{{.Status}}"
```

```bash
docker logs --tail 50 <container_name>
```

```bash
systemctl list-timers --no-pager
```

```bash
df -h / /mnt/hdd/immich /mnt/hdd/owncloud
```

---

## A service won't open

### Check layer by layer, from the bottom up

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:2283/api/server/ping
```

| Result | Where the problem is |
|---|---|
| `200` | the service is alive, look at the proxy or DNS |
| `000` / refused | the service is down, check `docker logs` |

If the service answers locally, check the path over the tailnet (current
`<tailscale-ip>` — [ENVIRONMENT.md](../ENVIRONMENT.md)):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<tailscale-ip>:2283/api/server/ping
```

Nothing at all — the port never bound to the tailnet address (see the next
section).

Then the fallback route around the proxy (current `<tailscale-hostname>` —
same file):

```
https://<tailscale-hostname>
```

If that works but the domain doesn't, the problem's in Nginx Proxy Manager
or DNS — the server itself is fine.

---

## Containers didn't come up after a reboot

**Symptom:** Docker's log says `cannot assign requested address`.

**Cause:** Docker started before Tailscale brought its interface up, and
couldn't bind to the tailnet address (see [ENVIRONMENT.md](../ENVIRONMENT.md)).

**Check:**

```bash
sysctl net.ipv4.ip_nonlocal_bind
```

Should read `1`. If it's `0`, restore it:

```bash
echo "net.ipv4.ip_nonlocal_bind = 1" | sudo tee /etc/sysctl.d/99-nonlocal-bind.conf
```

```bash
sudo sysctl -p /etc/sysctl.d/99-nonlocal-bind.conf
```

There should also be a drop-in enforcing startup order:

```bash
cat /etc/systemd/system/docker.service.d/10-after-tailscale.conf
```

---

## ownCloud: "Access through untrusted domain"

The domain isn't on the trusted list.

```bash
docker exec -u www-data owncloud_server occ config:system:get trusted_domains
```

Add it **via the environment variable in compose**, not through `occ`:
`OWNCLOUD_TRUSTED_DOMAINS` in `owncloud/.env`, then:

```bash
cd ~/Citadel/drive/owncloud && docker compose up -d
```

---

## ownCloud: settings revert after a restart

**Symptom:** changed something with `occ`, restarted the container, it's
back to how it was.

**Cause:** the image's entrypoint **rebuilds `config.php` from environment
variables on every start**, wiping out manual edits.

**Fix:** change it through variables in `docker-compose.yml` instead. See
the supported list:

```bash
docker exec owncloud_server grep -n "getenv" /etc/templates/config.php
```

That's how `OWNCLOUD_OVERWRITE_PROTOCOL`, `OWNCLOUD_TRUSTED_PROXIES`,
`OWNCLOUD_LOG_ROTATE_SIZE`, and the rest are configured.

**Exception:** a value that's already made it into `config.php` doesn't
disappear on its own just because the variable's gone. Delete it explicitly:

```bash
docker exec -u www-data owncloud_server occ config:system:delete overwritehost
```

---

## ownCloud: links point to http instead of https

TLS terminates at the proxy, and without a hint ownCloud assumes it's
running over plain HTTP.

Should be set to:

```bash
docker exec -u www-data owncloud_server occ config:system:get overwriteprotocol
```

Expected: `https`. Set via the `OWNCLOUD_OVERWRITE_PROTOCOL` variable.

**Important:** `overwritehost` is deliberately **not** set. The service is
reachable under two names at once, and pinning it to one would break the
other.

---

## ownCloud: large file uploads cut off

nginx's request body size limit. In Nginx Proxy Manager → **Advanced**:

```
client_max_body_size 0;
proxy_read_timeout 600s;
proxy_send_timeout 600s;
send_timeout 600s;
```

For Immich, also add `proxy_request_buffering off;` — otherwise nginx
buffers the whole file to disk first before passing it along. On a
gigabyte-scale video that's a needless wait and a needless write.

---

## MariaDB stuck in a restart loop

**Symptom:** `unknown variable 'read-only-compressed=OFF'`.

**Cause:** the flag was removed in MariaDB 10.11, but it's still in
ownCloud's official example compose, written for 10.6.

**Fix:** drop the flag from `command:`. Already fixed in this repo.

General approach to any database startup error:

```bash
docker logs owncloud_mariadb 2>&1 | tail -30
```

---

## Immich: forgot the password

**A regular user's password** — reset it as an admin:
**Administration → Users → the user → reset password**.

**The administrator's password:**

```bash
docker exec -it immich_server immich-admin reset-admin-password
```

List users and which one is the admin:

```bash
docker exec immich_postgres psql -U postgres -d immich -c 'select name, email, "isAdmin" from "user";'
```

---

## Immich: photos uploaded, but faces and search don't work

Background processing is either still running or stuck. **Administration →
Jobs** shows the queues and lets you restart a specific one.

If the queues are empty but there's no result, check the machine-learning
module:

```bash
docker logs --tail 50 immich_machine_learning
```

During a bulk import, processing takes hours and loads every core. That's
expected.

---

## Google Drive files aren't showing up in ownCloud

ownCloud only knows about files it put there itself. Anything copied in from
outside needs indexing:

```bash
docker exec -u www-data owncloud_server occ files:scan --path=/myxa3k/files/GoogleDrive
```

The sync script does this automatically at the end of its run. **During the
first copy, the folder will look empty in the UI** — the scan only runs
after the copy finishes.

Check whether the sync is even running:

```bash
systemctl is-active gdrive-sync.service
```

```bash
tail -20 /var/log/gdrive-sync.log
```

---

## rclone: Google authorization errors

| Symptom | Cause | Fix |
|---|---|---|
| `403: access_denied` | account isn't on the tester list | Google Auth Platform → Audience → Test users → add your email |
| Stopped working exactly a week later | the app is in Testing status — the refresh token lives 7 days | publish the app (In production) or reissue the token |
| `shared client_id is being retired` | using rclone's shared key | create your own OAuth client ID |
| Slow crawling through files | the shared rclone key is quota-throttled | same fix |

Reissuing the token happens on a machine with a browser:

```bash
rclone authorize "drive" --drive-client-id YOUR_ID --drive-client-secret YOUR_SECRET
```

The result goes into the `token` field of
`/root/.config/rclone/rclone.conf`.

**Important:** only ever open a fresh authorization link. Reloading an old
tab gives a stale one-time code, and rclone rejects it with
`Expecting "..." got "..."`.

---

## The backup isn't running

```bash
systemctl status restic-backup@personal.service
```

```bash
journalctl -u restic-backup@personal.service -n 50 --no-pager
```

Common causes:

| Symptom | Cause |
|---|---|
| `unable to open cache` | `RESTIC_CACHE_DIR` isn't set in the unit (systemd doesn't set `HOME`) |
| `repository is already locked` | a previous run died mid-way |
| errors from Backblaze | ran out of the free tier, or the key got revoked |

Clear a stuck lock:

```bash
set -a; . /etc/restic/personal.env; set +a
restic unlock
```

Run it by hand:

```bash
sudo systemctl start restic-backup@personal.service
```

---

## Running out of space

```bash
df -h /
```

```bash
sudo ncdu /
```

Check these first:

| Where | Expected |
|---|---|
| `/var/lib/docker` | logs capped at 10 MB × 3 per container |
| `/mnt/hdd/immich/backups` | Immich cleans up after itself |
| `/mnt/hdd/owncloud/backups` | last 7 dumps kept |
| `/var/log/gdrive-sync.log` | grows over time, trim if needed |

Remove unused Docker images:

```bash
docker image prune -a
```

---

## Upgrading services

Versions are pinned on purpose. An upgrade is a deliberate action:

1. Read the release notes, especially for database migrations
2. Confirm last night's backup ran
3. Bump the version in `.env`
4. `docker compose pull && docker compose up -d`
5. Check logs and confirm it's working

Rolling back on failure: put the old version back in `.env`, bring the stack
up, restore a database dump if needed (migrations are usually one-way).

**Debian's security updates install themselves.** Packages from third-party
repos — Docker and Tailscale — don't:

```bash
sudo apt update && sudo apt upgrade
```

A kernel update needs a reboot afterward. `health-watch` flags it on login.

---

## Pitfalls that cost real time

Collected from actually setting this up — so nobody hits them twice.

| Pitfall | The gist |
|---|---|
| `tailscale serve --http` | routes by hostname, returns 404 on a `Host` it doesn't recognize — no good for a third-party proxy, needs a direct port bind instead |
| `.local` domains | reserved for mDNS, don't resolve through normal DNS on macOS or iOS — use `.home.arpa` or your own domain |
| `pkill -f "rclone size"` | the pattern matches its own command line, kills itself — search by PID instead |
| ext4 on data disks | the default 5% reserve wastes ~100 GB on a pair of terabyte disks — format with `-m 1` |
| vzdump onto the root partition | the VM's archive doesn't fit in a small root partition, the job fails every night leaving nothing but logs |
| Live database directories in the backup | a torn copy that looks like it works; that's what dumps are for |
| `systemctl is-active --quiet` | also reports failure for `activating`, breaking wait loops that check it |
| Filesystem freeze during `vzdump` | usually a fraction of a second, but under heavy concurrent writes it can stretch to tens of seconds — a long-running process can drop network connections in that window |
| Unbounded Docker logs | a crash-looping container can fill the root partition in hours |
