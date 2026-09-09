# Telegram — notifications for the whole Citadel

Not tied to `drive/`, not tied to any single VM. This is a separate,
standalone infrastructure service: **one bot, one group with topics**, and
any host in the Citadel can push into it by copying a single file.

If you're Claude and you've been asked to set up monitoring on **another**
machine (the Proxmox host, another LXC, another VM) — read this file, not
`drive/`'s. `drive/` only knows about its own checks (Immich/ownCloud disks
and containers); it knows nothing about the delivery mechanics itself — all
of that lives here.

---

## Principle: no secrets in prose

Nothing below is a real bot token, group ID, or topic ID. Every current
value for this install lives **only** in `/etc/telegram-notify.env` on each
host that can send messages here. Simple reason: the bot gets rebuilt, a
topic gets renamed, and anything written in prose as a "fact" goes stale.
The `getUpdates` call (below) always shows the current values — the docs
don't repeat them.

---

## Architecture

```
any host  →  /usr/local/bin/notify.sh "text" [ok|warning|critical] [category]
                              │
                              ▼
                  https://api.telegram.org/bot<TOKEN>/sendMessage
                              │
                              ▼
                forum group with topics → the right topic (or General, if
                                            no topic is assigned to that category)
```

`notify.sh` is a standalone file with no dependency beyond `curl`. It reads
`/etc/telegram-notify.env`; if the file is missing or empty, it **quietly
does nothing** (`exit 0`). That's deliberate: the script can sit on a
machine where the token isn't configured yet without getting in anyone's
way or breaking whatever calls it.

### Why messages don't fire on every little thing

If a check finds the same problem hour after hour, sending the identical
message every time is a sure way to teach a human to ignore the bot
entirely. So *deciding whether to send* isn't `notify.sh`'s job (it just
sends whatever it's handed) — it's the **calling script's** job: it has to
remember its previous state and only call `notify.sh` when that state
changes. See `health-watch.sh` in `drive/scripts/` for an example of that
de-dup, done via a hash of each category's findings text.

### The gap this can't close on its own: silence ≠ "all fine"

If the monitoring process itself dies, or a host loses network, there won't
be any messages at all — and that's indistinguishable from "everything's
fine." Neither `notify.sh` nor a script calling it can close this gap on its
own — they can only stay silent because they themselves broke.

The fix is an external dead-man's-switch, and it's already wired up for
`drive/scripts/health-watch.sh` as a worked example: on every unattended
run, the script pings a URL from `/etc/healthchecks-ping.env`
(unconditionally, regardless of what it found — see
[`drive/healthchecks-ping.env.example`](../drive/healthchecks-ping.env.example)
and `docs/backups.md`'s "Who watches the watcher" for the reasoning).
[healthchecks.io](https://healthchecks.io) (free tier) raises its own alert
if that ping doesn't arrive on schedule — independent of whether this bot,
this chat, or the host itself is even alive.

The same pattern generalizes to any other periodic job in the Citadel, not
just this one: a scheduled check pings an external service unconditionally
at the end of every run; the ping cadence on the external side should match
the job's own schedule (a hard lesson learned the first time — set the
period too loose, like once a day for an hourly job, and the "watchdog"
takes almost a full day longer to notice a dead host than the job it's
watching would).

---

## Getting credentials (once, when the bot is created)

1. In Telegram, message **@BotFather** → `/newbot` → give it a name.
2. Create a **supergroup**, add the bot to it, turn on **Topics** in the
   group's settings ("Edit" → the **Topics** toggle).
3. Grab the group's `chat_id` and the `bot_token`:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   `chat_id` is a negative number like `-100...` in the `chat.id` field
   where `"type":"supergroup"`.

The bot's display name, avatar, and description can be changed directly
through the API, no BotFather needed (except the photo — that's only via
BotFather → `/mybots` → **Edit Botpic**):

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/setMyName" --data-urlencode "name=..."
curl -s "https://api.telegram.org/bot<TOKEN>/setMyDescription" --data-urlencode "description=..."
curl -s "https://api.telegram.org/bot<TOKEN>/setMyShortDescription" --data-urlencode "short_description=..."
```

**On Windows, pass text through a file** (`--data-urlencode "name@file"`)
rather than as a command-line argument — non-ASCII text going through argv
on a Windows console gets mangled by encoding easily; a UTF-8 file doesn't
have that problem.

---

## Topics

Forum mode on a group gives separate "tabs" inside one chat. Each has its
own `message_thread_id`.

### Finding a new topic's thread_id

1. Create the topic in the group.
2. Post `/id` in it — slash commands always reach the bot, even without
   admin rights and without privacy mode disabled.
3. Find that message's `message_thread_id` in `getUpdates`.
4. Add it to `/etc/telegram-notify.env`:
   ```
   TELEGRAM_TOPIC_<CATEGORY>=<thread_id>
   ```
   The variable name is the category, uppercased.

**The `General` topic is built in and can't be deleted** — a message with
no `message_thread_id` lands there automatically. So a category that
doesn't need its own topic can simply be left out of the env file — it
falls into the main stream on its own.

---

## Connecting a new host

1. Copy `notify.sh` to `/usr/local/bin/notify.sh` on that host, `chmod 755`.
2. Copy `notify.env.example` to `/etc/telegram-notify.env`, fill in the same
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (ask whoever set them up, or
   copy from an already-working host), `chmod 600`.
3. Call it from any script or systemd unit:
   ```bash
   /usr/local/bin/notify.sh "message text" warning
   ```

The message is automatically signed with the host's name (`hostname`), so
in a shared chat it's immediately clear which machine sent it.

## Adding a new category (a new service, a new check)

No changes to `notify.sh` are needed — all the work is on the caller's side:

1. Pick a category name: short, lowercase, ASCII (`system`, `immich`,
   `nginx`, `zfs` — anything descriptive).
2. Decide whether this category needs its own topic, or can fall into
   `General` / an existing one.
   - If it needs its own: create the topic, grab its `thread_id` (above),
     add `TELEGRAM_TOPIC_<NAME>=<id>` to the env file on the relevant hosts.
   - If not: add nothing, the category lands in the main stream on its own.
3. In the check script, call:
   ```bash
   /usr/local/bin/notify.sh "finding text" [ok|warning|critical] category_name
   ```

## Rule for picking a level (`ok`/`warning`/`critical`)

There's no single formula here, and there won't be — it's a manual call made
by whoever writes the specific check, expressed as thresholds inside that
check (see `drive/scripts/health-watch.sh` for an example). General rule:

```
critical  →  needs action right now (a service is down, a disk is full, a backup stopped)
warning   →  worth knowing, not an emergency (disk at 80%, a reboot will be needed soon)
ok        →  ONLY as a recovery message after critical/warning —
             never for routine "all fine" pings, or the bot turns into noise
```

## Category naming rule

A category is `TELEGRAM_TOPIC_<NAME_IN_UPPERCASE>` in the env file. The
category name passed to `notify.sh` is the same thing, lowercased. No other
registration is needed — if the variable doesn't exist, the category just
falls into the main stream, no errors, no warnings.
