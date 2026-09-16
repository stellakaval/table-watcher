# table-watcher

A Muse Code plugin that watches OpenTable restaurants for hard-to-get
reservations. It polls availability on a schedule, matches your time window and
party size, and notifies you on a hit — or books the table automatically if you
explicitly opt in.

## Features

- **Batch watching** — each run checks a small sequential batch and advances a
  rotating pointer, so the full (restaurant × date × party size) space is swept
  over successive hourly runs. One provider call at a time, paced, with
  automatic backoff on rate limits.
- **Auto-book (opt-in)** — books at most **one** table per run: the
  highest-priority hit (your restaurant ranking, then earliest date/time).
  Lower-priority hits notify only, so you never double-book yourself.
- **Scout reports** — `--scout` sweeps the entire range and writes a Markdown
  availability report (`logs/scout_YYYY-MM-DD.md`): which dates have tables,
  per restaurant and party size. Run it weekly for discovery.
- **Notifications that reach you** — hits fan out to a shell command, an
  [ntfy.sh](https://ntfy.sh) topic (push alerts, no account), and/or a JSON
  webhook (Discord, Slack, Zapier, your own service).
- **Operational commands** — `--status` shows queue progress and last hits;
  `--cancel` cancels a reservation by confirmation ID.
- **Provider architecture** — OpenTable ships today; Resy/Tock plug into the
  same `check()`/`book()` interface when their CLIs exist.

## How it compares

| | table-watcher | table-scout | opentable-mcp | resy-notifier |
|---|---|---|---|---|
| Install | Muse Code plugin (`/plugins`) | manual script + launchd | MCP server | manual script |
| Platforms | OpenTable (Resy/Tock-ready) | Resy, OpenTable, Tock | OpenTable | Resy |
| Auto-book | yes, one best hit per run | no (alerts only) | via MCP tools | no |
| Discovery report | yes (`--scout`) | yes (weekly email) | no | no |
| Notifications | shell, ntfy, webhook | email | client-dependent | ntfy |
| Cancel reservations | yes (`--cancel`) | no | via MCP tools | no |

## How it works

1. You configure the restaurants (by OpenTable `rid`), party sizes, date range,
   and time window in `scripts/config.json`.
2. `scripts/watch.py` checks a small batch of (restaurant × date × party size)
   combos each run via the `opentable` CLI, keeping a rotating pointer in
   `state/` so successive runs sweep the whole space.
3. On a hit it notifies across every configured channel (default), or attempts
   a booking (`auto_book: true`, opt-in only).

## Install

**As a local bundle (fastest):**

```
/plugin marketplace add /path/to/table-watcher
/plugin install table-watcher@table-watcher
```

**As a marketplace repo:** push this repo to GitHub, then

```
/plugin marketplace add YOUR-USERNAME/table-watcher
/plugin install table-watcher@table-watcher
```

Then use `/watch-table <restaurant name>` to set up a watch.

## Quickstart

```bash
# 1. Copy the example config and fill in your details
cp scripts/config.example.json scripts/config.json
# edit: restaurants (rid via `opentable lookup-rid`), party sizes,
# date range, time window, diner details, notify_command

# 2. Make sure OpenTable is connected
opentable status   # if not_connected, follow the connect link it prints

# 3. Dry-run one batch
python3 scripts/watch.py --config scripts/config.json

# 4. Schedule it (hourly is a good default)
crontab -e
# 0 * * * * /usr/bin/python3 /path/to/table-watcher/scripts/watch.py --config /path/to/table-watcher/scripts/config.json

# 5. Optional: weekly scout report (Monday mornings)
# 0 9 * * 1 /usr/bin/python3 /path/to/table-watcher/scripts/watch.py --config /path/to/table-watcher/scripts/config.json --scout

# 6. Check status anytime
python3 scripts/watch.py --config scripts/config.json --status
```

Find a restaurant's `rid` with:

```bash
opentable lookup-rid --name "House of Prime Rib" --city "San Francisco"
```

## Auto-book: explicit opt-in only

`auto_book` defaults to `false` (notify-only). If you set it to `true`:

- The watcher will book **at most one table per run** — the best hit, chosen by
  your restaurant priority first, then earliest date/time. Lower-priority hits
  notify only, so one run can never double-book you. No further confirmation.
  Only enable it for watches where any matching slot is acceptable to you.
- You must fill in `diner` (first name, last name, email, phone) in
  `config.json`.
- A booking only counts when the script reports `confirmed`. Anything else —
  errors, empty output, timeouts — is reported as a miss, never as a booking.
- A confirmed booking fires the notification channels too (so your phone still
  buzzes with the details).

## Recording a good demo

1. Set up a watch in notify-only mode for a restaurant you know is fully booked
   (or temporarily narrow the date range to force quick coverage).
2. Start recording. Run `python3 scripts/watch.py` in a terminal.
3. Show the JSONL log / summary: batch scanned, hit found, notification fired.
4. For the auto-book version: run with `auto_book: true` on a low-stakes
   restaurant and show the `CONFIRMED` line — then immediately cancel the
   reservation (`opentable cancel-reservation`) so the demo doesn't hold a
   real table.

## Honest limitations

- **OpenTable only.** Restaurants not listed on OpenTable can't be watched —
  the plugin tells you this instead of pretending.
- **No waitlist support.** It polls public availability; it can't join or
  monitor a restaurant's internal waitlist.
- **Card-required slots can't be booked** by the connector (deposits /
  prepayments). You'll get a notification and a pointer to book directly.
- **Polling, not push.** Slots can appear and vanish between checks; a hit is
  "a table was open when we looked," not a hold.
- **Rate limits are respected, not dodged.** One call at a time, paced batches,
  automatic backoff on 429. Don't run parallel watchers.
- **Party sizes 1–20** (connector limit). Larger parties need the restaurant
  directly.
