---
name: table-watcher
description: Watch OpenTable restaurants for hard-to-get reservations. Polls availability on a schedule, matches a time window and party size, notifies on hits, and can auto-book with explicit opt-in. Use when someone wants to snag a table at a fully-booked restaurant.
---

# table-watcher

Watches one or more OpenTable restaurants for availability inside a time window
and party size, then notifies (default) or books (opt-in) when a slot opens.
Backed by `scripts/watch.py`, which drives the `opentable` CLI.

## Setting up a watch

1. **Resolve the restaurant to an `rid`.**
   Run `opentable lookup-rid` with the restaurant name; include `--city` when the
   city is known. If there is no clear match, try name variants or drop filters.
   If `lookup-rid` finds nothing after reasonable retries, stop: the venue is
   not on OpenTable and cannot be watched by this plugin. Say so plainly and
   suggest the venue's own site or another platform.

2. **Confirm the watch parameters with the user** — party size (1–20, the
   connector's limit), date range, and time window (e.g. 18:00–19:30). Also
   confirm the mode:
   - **notify-only (default, recommended):** the watcher pings on a hit and the
     user books it themselves.
   - **auto-book (opt-in):** the watcher books the first in-window slot it
     finds. Requires the diner's first name, last name, email, and phone, and
     explicit user authorization for each watch. Never enable silently.

3. **Write the config.** Copy `scripts/config.example.json` to
   `scripts/config.json` and fill in restaurants (rid + priority), party sizes,
   time window, date range, diner details (auto-book only), and an optional
   `notify_command` (a shell command; `{restaurant}`, `{date}`, `{time}`,
   `{party_size}`, `{rid}` are substituted). Never commit `config.json`.

4. **Check the OpenTable connection.** Run `opentable status` first. If it is
   `not_connected`, post the returned `connect_url` so the user can connect.
   Do not invent the URL.

5. **Dry-run once.** Run `python3 scripts/watch.py --config scripts/config.json`
   and confirm it scans without errors and the state file appears under
   `state/`.

6. **Schedule it.** Set up an hourly cron (or equivalent scheduler) running
   `python3 <repo>/scripts/watch.py --config <repo>/scripts/config.json`.
   Each run checks a small batch and advances a rotating pointer, so the full
   (restaurant × date × party size) space is swept over successive runs.
   Optionally schedule a weekly `--scout` run for a full-range report.

## Modes (`scripts/watch.py`)

- **Default (batch):** one small sequential batch per run; advances the rotating
  pointer. Cron-friendly. Books at most ONE table per run in auto-book mode:
  the highest-priority hit (restaurant priority, then earliest date/time).
  Lower-priority hits notify only.
- **`--scout`:** sweeps the ENTIRE queue and writes a Markdown availability
  report to `logs/scout_YYYY-MM-DD.md` (plus JSON) — which dates have
  in-window tables, per restaurant and party size. Good as a weekly discovery
  run (e.g. Monday mornings).
- **`--status`:** prints queue progress (% swept), last run summary, last hits,
  rate-limit state, and far-out skips. Read-only.
- **`--cancel --rid RID --confirmation-id ID`:** cancels a reservation. Prints
  the result; only counts when the CLI confirms the cancellation.

## Notifications

Hits (and confirmed bookings) fan out across every configured channel:

- `notify_command`: a shell command; `{restaurant}`, `{date}`, `{time}`,
  `{party_size}`, `{rid}` are substituted.
- `notify_ntfy_topic`: posts to `https://ntfy.sh/<topic>` — no account needed,
  install the ntfy app to get push alerts anywhere.
- `notify_webhook_url`: POSTs a JSON event
  (`table_watcher.hit` / `table_watcher.booking`) — wire it to Discord,
  Slack, Zapier, or your own service.

## Provider model

Availability checking and booking go through a provider object; only
`"opentable"` ships (it wraps the `opentable` CLI). Each restaurant entry
accepts an optional `"provider"` key (default `"opentable"`). A Resy/Tock
provider is a class with the same `check()`/`book()` interface, registered in
`PROVIDERS` in `watch.py` once their CLI or API exists in the runtime.

## On a hit

- In notify-only mode: tell the user the exact restaurant, date, time, and
  party size, attribute the availability to OpenTable, and let them book.
- In auto-book mode: the script already attempted the booking. **Only tell the
  user a booking went through when the script reports `confirmed`.** If the
  booking failed or came back empty, say so plainly — never invent a
  confirmation or confirmation number.

## Rules (from the OpenTable connector contract)

- One OpenTable call at a time: the script is sequential with pauses between
  calls. Do not run multiple watchers or parallel batches against OpenTable.
- If a run reports rate limiting (exit code 2), do not retry in the same task.
  The script backs off automatically; the next scheduled run resumes.
- Times from OpenTable without a timezone offset are restaurant-local civil
  time. Do not convert or guess timezones.
- The connector cannot book slots requiring a card, deposit, or prepayment
  (flagged via `cancellation_policy` / `prePaymentRequired`). On a hit for such
  a slot, notify the user and point them at the restaurant's own booking page.
- Surface `no_availability_reasons` in plain language, not raw codes.
- Only add special requests the user explicitly gave (allergies, seating).
