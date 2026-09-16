---
description: Set up a watch for tables at an OpenTable restaurant (notify or auto-book on hits)
argument-hint: <restaurant name> [--party-size N] [--dates START..END] [--auto-book]
---

# /watch-table

Set up an availability watch for a restaurant using the table-watcher skill.

## Steps

1. Parse arguments: the restaurant name is required. Optional flags:
   `--party-size N` (default: ask), `--dates YYYY-MM-DD..YYYY-MM-DD`
   (default: ask), `--auto-book` (default: notify-only).
2. Read `skills/table-watcher/SKILL.md` and follow it exactly:
   - Resolve the restaurant to an `rid` with `opentable lookup-rid`.
   - Confirm party size, date range, time window, and notify vs auto-book mode
     with the user. Auto-book requires explicit opt-in plus diner name, email,
     and phone — never enable it from a flag alone without confirming.
   - Write `scripts/config.json` from `scripts/config.example.json`.
   - Run `opentable status`; if not connected, share the connect link.
   - Dry-run `python3 scripts/watch.py --config scripts/config.json`.
   - Schedule hourly runs (cron or equivalent) and confirm the schedule.
3. Mention the extras: `python3 scripts/watch.py --scout` for a weekly
   full-range availability report, `--status` for queue progress, and
   `--cancel --rid RID --confirmation-id ID` to cancel a reservation.
4. Report back what is being watched (restaurant, party size, dates, window,
   mode, check frequency) in plain language.
