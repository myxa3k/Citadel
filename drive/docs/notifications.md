# Notifications from the drive service

The delivery mechanics (the bot, topics, `notify.sh`, how to wire up a new
host) aren't `drive`'s concern — they're shared across the whole Citadel and
documented in [`telegram/README.md`](../../telegram/README.md) at the repo
root.

What's here is only what's specific to this service: how `health-watch.sh`
sorts its findings into categories.

## drive's categories

`scripts/health-watch.sh` classifies findings into four categories and
notifies on each **independently** (separate state, separate de-dup) — an
ownCloud problem never drowns out, or gets confused with, an Immich one:

```
/ disk pressure                → system
/mnt/hdd/immich disk pressure    → immich
/mnt/hdd/owncloud disk pressure   → owncloud
immich_* container                → immich
owncloud_* container               → owncloud
any other container                → system
restic, gdrive-sync,               → backups
  uncovered accounts
reboot required                   → system
```

Which Telegram topic a category maps to is decided **on this host**, in
`/etc/telegram-notify.env` (`TELEGRAM_TOPIC_IMMICH` and so on). How to find
and change that — see `telegram/README.md`.

## Severity

The Telegram icon (🚨/⚠️/🛡️) comes from the **worst** label actually present
in the text: if a category has even one `CRITICAL:` line, the level is
`critical`, otherwise `warning`. The thresholds themselves (what counts as
`CRITICAL` vs `WARNING`) live right in `health-watch.sh`'s code, decided
per-check — there's no single severity formula, just manual judgment calls
expressed as thresholds.
