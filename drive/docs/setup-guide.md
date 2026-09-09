# Setting up from scratch

Step-by-step install of this same system on a clean machine. Written for
someone comfortable with a terminal, but not expected to already know
Proxmox, Docker, or ZFS.

If you're handing this to an AI assistant, start with [CLAUDE.md](../CLAUDE.md).

---

## What you'll need

| | Minimum | Recommended |
|---|---|---|
| CPU cores | 2 | **4** |
| RAM | 6 GB | **8 GB** |
| System disk | 40 GB | **100 GB, on SSD/NVMe** |
| Photo disk | library size + 10% | separate volume |
| Files disk | size + headroom | separate volume |

Plus a Tailscale account (free) and an account with a cloud storage provider
for backups.

**The system disk must be an SSD.** The databases live there, and search in
Immich turns painful on a spinning disk.

---

## Step 1. Virtual machine

If installing under Proxmox:

```
CPU type             host          (need AVX for machine learning)
Cores                4
Memory               8192 MB, balloon off
SCSI Controller      VirtIO SCSI single
System disk          100 GB on SSD storage, Discard on
Network              VirtIO
QEMU Guest Agent     enabled
```

**Turning off the memory balloon is mandatory.** Postgres and Redis handle
memory being reclaimed live badly, and hit OOM at the worst possible moment.

Attach the data disks as separate devices with `discard=on`. On thin storage,
without discard deleted files never actually give the space back.

---

## Step 2. Debian

Install **Debian 13 (netinst)**. During setup:

- Partitioning: **Guided → use entire disk → all files in one partition**,
  and pick the system disk specifically. Leave the data disks alone.
- **Leave the root password blank** — the installer then sets up `sudo` for
  your user. Set a root password and `sudo` won't be installed at all.
- Software selection: uncheck everything, keep only **SSH server** and
  **standard system utilities**.

Don't carve out a separate `/var`: Docker lives there, and a fixed size will
eventually run out while other partitions still have room.

After the first boot:

```bash
sudo apt update && sudo apt install -y curl ca-certificates git jq qemu-guest-agent
```

---

## Step 3. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
```

```bash
sudo tailscale up
```

Open the link it gives you and authorize the machine. Then let yourself
manage Tailscale without sudo:

```bash
sudo tailscale set --operator=$USER
```

In the Tailscale admin console, enable **MagicDNS** and **HTTPS
Certificates** — without them there's no valid TLS certificate.

---

## Step 4. Data disks

Find them and **make sure they're empty**:

```bash
lsblk -f
```

```bash
sudo wipefs /dev/sdb
```

An empty result from the second command means there's no filesystem
signature on it.

Partition and format (replace `sdb` with your actual device):

```bash
printf 'label: gpt\nstart=2048, type=linux, name="immich"\n' | sudo sfdisk /dev/sdb
```

```bash
sudo mkfs.ext4 -m 1 -L immich /dev/sdb1
```

`-m 1` instead of the default 5% reserve saves about 50 GB on a
terabyte-class disk.

Mount by UUID:

```bash
echo "UUID=$(sudo blkid -s UUID -o value /dev/sdb1) /mnt/hdd/immich ext4 defaults,noatime,nofail,x-systemd.device-timeout=10 0 2" | sudo tee -a /etc/fstab
```

```bash
sudo mkdir -p /mnt/hdd/immich && sudo systemctl daemon-reload && sudo mount -a
```

Repeat for the second disk, labeled `owncloud`.

**Why it's done this way:** a UUID doesn't change when disks get shuffled
around, `nofail` stops a missing disk from dropping the boot into a rescue
console, `noatime` cuts a needless write on every file read.

---

## Step 5. Docker

From the official repository — Debian's own package is significantly older:

```bash
sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
```

```bash
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

```bash
sudo usermod -aG docker $USER && newgrp docker
```

Cap the logs, or one crash-looping container will fill the disk:

```bash
printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n' | sudo tee /etc/docker/daemon.json
```

```bash
sudo systemctl restart docker
```

---

## Step 6. Services

Clone this repository and fill in the settings:

```bash
git clone <repo-url> ~/Citadel && cd ~/Citadel/drive
```

```bash
cp immich/.env.example immich/.env && cp owncloud/.env.example owncloud/.env
```

Open both `.env` files and set the passwords. To generate a strong one:

```bash
tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32; echo
```

In `immich/.env` the database password must be **letters and digits only** —
that's an Immich requirement.

Lock the files down and start:

```bash
chmod 600 immich/.env owncloud/.env
```

```bash
cd immich && docker compose up -d
```

```bash
cd ../owncloud && docker compose up -d
```

The first run pulls several gigabytes of images. Check:

```bash
docker ps
```

Every container should show `healthy`.

---

## Step 7. HTTPS access

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:2283
```

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8080
```

Done: the services are now reachable at
`https://<machine-name>.<your-tailnet>.ts.net` with a real certificate. Find
your machine's name:

```bash
tailscale status --json | jq -r .Self.DNSName
```

Open Immich in a browser — **whoever registers first becomes the
administrator**.

### Your own domain via a reverse proxy (optional)

For addresses like `photos.example.com`, bind the ports in compose to the
Tailscale address too and point a proxy at it:

```yaml
ports:
  - '127.0.0.1:2283:2283'
  - '<your-tailscale-ip>:2283:2283'
```

```bash
echo "net.ipv4.ip_nonlocal_bind = 1" | sudo tee /etc/sysctl.d/99-nonlocal-bind.conf && sudo sysctl -p /etc/sysctl.d/99-nonlocal-bind.conf
```

Without that last line, the container won't start on boot if Docker wins the
race against Tailscale coming up.

In the proxy, don't forget `client_max_body_size 0` and generous timeouts —
otherwise large videos won't upload.

---

## Step 8. Backups

The system isn't production-ready without this step.

```bash
sudo apt install -y restic
```

Create a private bucket with your cloud storage provider and an access key
scoped to just that bucket. In the bucket's settings, enable **keep only the
last version of a file** — otherwise blocks deleted during rotation stay on
your bill forever.

```bash
sudo install -d -m 700 /etc/restic
```

Create `/etc/restic/personal.env` (mode `600`):

```
RESTIC_REPOSITORY=b2:<bucket>:restic
B2_ACCOUNT_ID=<keyID>
B2_ACCOUNT_KEY=<applicationKey>
RESTIC_PASSWORD=<long random password>
```

And `/etc/restic/personal.conf` — whose data belongs to this profile:

```
IMMICH_UIDS="<Immich user uuid>"
OC_USERS="<ownCloud login>"
```

Immich user UUID:

```bash
docker exec immich_postgres psql -U postgres -d immich -t -c 'select id, name from "user";'
```

Install the scripts and units from this repo:

```bash
sudo install -m 0700 scripts/*.sh /usr/local/bin/
```

```bash
sudo install -m 0644 systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now owncloud-db-backup.timer restic-backup@personal.timer restic-check@personal.timer health-watch.timer
```

Initialize the repository and take the first backup:

```bash
set -a; . /etc/restic/personal.env; set +a; sudo -E restic init
```

```bash
sudo systemctl start restic-backup@personal.service
```

> ### Verify restore — mandatory
>
> ```bash
> sudo -E restic restore latest --target /tmp/restore-test --include <path-to-any-file>
> ```
>
> Compare checksums against the original. Until you've done this, you don't
> have a backup — you have hope of a backup.

**Write the repository password down off the server.** Losing it means the
data is unreadable by anyone, forever.

For alerts (a stopped container, a stale backup) wired into Telegram —
separate service, see [`telegram/README.md`](../../telegram/README.md).

---

## Step 9. Maintenance

Automatic security updates:

```bash
sudo apt install -y unattended-upgrades
```

```bash
sudo systemctl enable --now unattended-upgrades
```

Don't enable automatic reboots: a server holding a photo archive shouldn't
drop offline without warning. `health-watch` will flag it when one's needed.

Warnings on SSH login:

```bash
sudo ln -sf /usr/local/bin/health-watch.sh /etc/update-motd.d/99-health-watch
```

---

## Checklist before calling it done

- [ ] Every container is `healthy`
- [ ] Both services load over HTTPS with no certificate warnings
- [ ] The mobile app connects
- [ ] `df -h` — disks are mounted, there's enough room
- [ ] Every timer is `enabled` and `active`
- [ ] The first backup ran, **and restore has been verified**
- [ ] The repository password is written down off the server
- [ ] **The machine was rebooted, and everything came back up on its own**

The last item is non-negotiable. A setup that doesn't survive a reboot isn't
a setup.
