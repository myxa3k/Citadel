# Current values for this install

The one file other docs point to instead of repeating instance-specific
facts in prose. Rather than list values that go stale the moment the VM is
rebuilt (and that a public repo doesn't need to publish anyway), this file
lists how to look each one up live, on the machine itself.

If a doc elsewhere says "see ENVIRONMENT.md" for an IP, hostname, or size,
this is where to run the command, not read a number.

---

## Network

| What | How to check |
|---|---|
| VM's Tailscale address | `tailscale ip -4` |
| VM's tailnet hostname | `tailscale status --json \| jq -r .Self.DNSName` |
| Tailnet suffix | `tailscale status --json \| jq -r .MagicDNSSuffix` |
| Public domain + subdomains | the DNS zone / Nginx Proxy Manager config on the LXC host below |

## Proxmox

| What | How to check |
|---|---|
| Proxmox host | ask the owner, or check `/etc/hostname` on the host |
| This stack's VM | `qm list` on the host — look for the one running this stack |
| LXC running Nginx Proxy Manager | `pct list` on the host |
| LXC running Grafana | `pct list` on the host |

LXC/VM topology can change; always reconfirm with the commands above rather
than trusting a remembered number.

## Disks (inside the VM)

| Mount point | How to check |
|---|---|
| `/` (system, databases) | `df -h /` |
| `/mnt/hdd/immich` | `df -h /mnt/hdd/immich` |
| `/mnt/hdd/owncloud` | `df -h /mnt/hdd/owncloud` |

Exact UUIDs — `blkid`, valid only on this specific VM, don't carry them into
prose (see `docs/architecture.md` for why mounting is done by UUID rather
than device name in the first place).

## Backups

| What | How to check |
|---|---|
| Provider | Backblaze B2 |
| Bucket + path | `/etc/restic/personal.env` on the VM, root-only, not in git |
| restic profile(s) | `ls /etc/restic/*.conf` on the VM |
| Keys and password | `/etc/restic/personal.env` on the VM itself, **not in git** |

## Users

| Immich / ownCloud login | Role |
|---|---|
| `shiro` | administrator, holds no photos or files of their own |
| `myxa3k` | regular user, owns the `personal` backup profile |

## Notifications (Telegram)

The mechanics are in [`telegram/README.md`](../telegram/README.md). The
actual token, `chat_id`, and topic IDs live **only** in
`/etc/telegram-notify.env` on hosts that can send messages; they're
deliberately absent from this file — recreated on a bot/group change without
touching a single line of documentation.
