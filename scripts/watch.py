#!/usr/bin/env python3
"""
table-watcher: poll OpenTable availability for hard-to-get restaurants.

Modes (one per invocation):
  (default)   One small sequential batch of availability checks (cron-friendly).
              Rotating pointer over the (restaurant x date x party-size) queue,
              JSONL logs, rate-limit handling, optional auto-book of the single
              best hit per run.
  --scout     Sweep the ENTIRE queue and write a Markdown availability report
              (which dates have in-window tables, per restaurant/party size).
              Weekly-cron friendly.
  --status    Print queue progress, last run summary, last hits, rate-limit state.
  --cancel    Cancel a reservation: --cancel --rid RID --confirmation-id ID.

Reads scripts/config.json. Stdlib only. Makes OpenTable CLI calls strictly one
at a time with a pause between them, and stops a run on any rate-limit signal
(HTTP 429).

Provider model: availability checking/booking goes through a Provider object.
Only "opentable" ships (it wraps the `opentable` CLI on PATH). A Resy or Tock
provider plugs in here once their CLI/API exists in the runtime — add a class
with check()/book() and register it in PROVIDERS.
"""
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.json")


def log(msg):
    print(msg, flush=True)


def run_cli(args, timeout):
    """Run one opentable CLI call. Returns (returncode, stdout, stderr, timed_out)."""
    try:
        p = subprocess.run(
            ["opentable"] + args,
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return p.returncode, p.stdout or "", p.stderr or "", False
    except subprocess.TimeoutExpired:
        return 124, "", "timeout", True
    except FileNotFoundError:
        log("ERROR: `opentable` CLI not found on PATH. Is the connector installed?")
        sys.exit(3)


def parse_cli_json(stdout, stderr):
    """The CLI prints JSON on stdout. Returns (parsed_dict_or_None, raw_text)."""
    raw = (stdout or "").strip() or (stderr or "").strip()
    if not raw:
        return None, raw
    try:
        return json.loads(stdout), raw
    except Exception:
        try:
            return json.loads(raw), raw
        except Exception:
            return None, raw[:2000]


def hhmm_of(slot):
    """Extract HH:MM from a slot that may be 'HH:MM', ISO datetime, or a dict."""
    if isinstance(slot, dict):
        slot = slot.get("date_time") or slot.get("time") or slot.get("datetime") or ""
    s = str(slot).strip()
    if "T" in s:
        s = s.split("T", 1)[1]
    return s[:5] if len(s) >= 5 and s[2] == ":" else ""


def iso_of(slot, date_str):
    """Normalize a slot to YYYY-MM-DDTHH:MM (restaurant-local civil time)."""
    if isinstance(slot, dict):
        slot = slot.get("date_time") or slot.get("time") or slot.get("datetime") or ""
    s = str(slot).strip()
    if "T" in s:
        date_part, time_part = s.split("T", 1)
        return "%sT%s" % (date_part[:10], time_part[:5])
    hm = hhmm_of(s)
    return "%sT%s" % (date_str, hm) if hm else ""


def in_window(hhmm, start, end):
    return bool(hhmm) and start <= hhmm <= end


def looks_rate_limited(rc, raw, parsed):
    text = (raw or "").lower()
    if rc == 429 or "429" in text or "rate limit" in text or "ratelimit" in text:
        return True
    if isinstance(parsed, dict):
        body = parsed.get("body") or {}
        reasons = body.get("no_availability_reasons") or []
        if any("rate" in str(r).lower() and "limit" in str(r).lower() for r in reasons):
            return True
    return False


def looks_far_out(parsed):
    """True when the venue's books simply aren't open that far out yet."""
    if not isinstance(parsed, dict):
        return False
    body = parsed.get("body") or {}
    reasons = [str(r).lower() for r in (body.get("no_availability_reasons") or [])]
    needles = ("not released", "too far", "not yet", "not available yet",
               "booking window", "not open")
    return any(n in r for r in reasons for n in needles)


def build_queue(cfg):
    """All (restaurant, date, party_size) combos, priority-ordered, future dates only."""
    today = datetime.date.today().isoformat()
    d0 = cfg["date_range"]["start"]
    d1 = cfg["date_range"]["end"]
    start = datetime.date.fromisoformat(d0)
    end = datetime.date.fromisoformat(d1)
    restaurants = sorted(cfg["restaurants"], key=lambda r: r.get("priority", 99))
    queue = []
    d = start
    while d <= end:
        ds = d.isoformat()
        if ds >= today:
            for r in restaurants:
                if not r.get("rid"):
                    continue
                for size in cfg["party_sizes"]:
                    queue.append({
                        "rid": r["rid"],
                        "name": r.get("name", str(r["rid"])),
                        "provider": r.get("provider", "opentable"),
                        "date": ds,
                        "party_size": size,
                    })
        d += datetime.timedelta(days=1)
    return queue


def fingerprint(cfg):
    blob = json.dumps({
        "restaurants": cfg["restaurants"],
        "party_sizes": cfg["party_sizes"],
        "date_range": cfg["date_range"],
        "time_window": cfg["time_window"],
    }, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def load_state(state_path, cfg):
    state = {"pointer": 0, "far_out": {}, "queue": [], "queue_fingerprint": "",
             "rate_limited_until": "", "last_hits": []}
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state.update(json.load(f))
        except Exception as e:
            log("WARN: could not read state file (%s); starting fresh" % e)
    fp = fingerprint(cfg)
    if state.get("queue_fingerprint") != fp or not state.get("queue"):
        state["queue"] = build_queue(cfg)
        state["queue_fingerprint"] = fp
        state["pointer"] = 0
        state["far_out"] = {}
        log("queue rebuilt: %d combos (fingerprint %s)" % (len(state["queue"]), fp))
    return state


def save_state(state_path, state):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- providers ---

def check_availability_opentable(combo, cfg):
    """One OpenTable availability check. Returns a result dict."""
    start = cfg["time_window"]["start"]
    minutes = max(1, (int(cfg["time_window"]["end"][:2]) * 60
                      + int(cfg["time_window"]["end"][3:5]))
                  - (int(start[:2]) * 60 + int(start[3:5])))
    cmd = ["search-availability",
           "--rid", str(combo["rid"]),
           "--start-date-time", "%sT%s" % (combo["date"], start),
           "--party-size", str(combo["party_size"]),
           "--forward-minutes", str(minutes)]
    t0 = time.time()
    rc, out, err, timed_out = run_cli(cmd, cfg.get("command_timeout_seconds", 25))
    parsed, raw = parse_cli_json(out, err)
    result = {"provider": "opentable", "rid": combo["rid"], "name": combo["name"],
              "date": combo["date"], "party_size": combo["party_size"], "rc": rc,
              "elapsed_s": int(time.time() - t0), "timed_out": timed_out,
              "in_window_slots": [], "reasons": [], "rate_limited": False,
              "far_out": False, "raw_preview": raw[:500]}
    if timed_out:
        result["reasons"] = ["cli_timeout"]
        return result
    if looks_rate_limited(rc, raw, parsed):
        result["rate_limited"] = True
        return result
    if parsed is None:
        result["reasons"] = ["unparseable_output"]
        return result
    body = parsed.get("body") or {}
    result["reasons"] = body.get("no_availability_reasons") or []
    for t in body.get("times") or []:
        hm = hhmm_of(t)
        if in_window(hm, cfg["time_window"]["start"], cfg["time_window"]["end"]):
            iso = iso_of(t, combo["date"])
            if iso:
                result["in_window_slots"].append(iso)
    if not result["in_window_slots"] and looks_far_out(parsed):
        result["far_out"] = True
    return result


def book_opentable(combo, slot_iso, cfg):
    """Attempt one OpenTable booking. Returns dict; only 'confirmed' counts."""
    diner = cfg.get("diner") or {}
    missing = [k for k in ("first_name", "last_name", "email", "phone")
               if not diner.get(k)]
    if missing:
        return {"confirmed": False, "error": "diner details incomplete: %s" % ",".join(missing)}
    cmd = ["book-reservation",
           "--rid", str(combo["rid"]),
           "--party-size", str(combo["party_size"]),
           "--date-time", slot_iso,
           "--first-name", diner["first_name"],
           "--last-name", diner["last_name"],
           "--email", diner["email"],
           "--phone-number", diner["phone"]]
    rc, out, err, timed_out = run_cli(cmd, cfg.get("command_timeout_seconds", 25))
    text = (out + "\n" + err)
    low = text.lower()
    confirmed = (rc == 0 and not timed_out
                 and ("confirm" in low or "reservation" in low))
    return {"confirmed": confirmed, "rc": rc, "timed_out": timed_out,
            "output_preview": text.strip()[:1500]}


class OpenTableProvider:
    """Provider wrapping the `opentable` CLI."""
    name = "opentable"

    def check(self, combo, cfg):
        return check_availability_opentable(combo, cfg)

    def book(self, combo, slot_iso, cfg):
        return book_opentable(combo, slot_iso, cfg)


# Register providers here. A Resy/Tock provider is a class with the same
# check()/book() interface, wrapping whatever CLI/API the runtime offers.
PROVIDERS = {"opentable": OpenTableProvider()}


def get_provider(name):
    p = PROVIDERS.get(name or "opentable")
    if p is None:
        raise ValueError("unknown provider %r (known: %s)"
                         % (name, ", ".join(sorted(PROVIDERS))))
    return p


# ------------------------------------------------------------ notifications ---

def notify_command(hit, cfg):
    """Run the configured shell notify command with hit details substituted."""
    template = (cfg.get("notify_command") or "").strip()
    if not template:
        return {"notified": False, "reason": "no notify_command configured"}
    mapping = {"{restaurant}": hit["name"], "{rid}": str(hit["rid"]),
               "{date}": hit["date"], "{time}": hit.get("slot", ""),
               "{party_size}": str(hit["party_size"])}
    cmd = template
    for k, v in mapping.items():
        cmd = cmd.replace(k, v)
    try:
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=30)
        return {"notified": p.returncode == 0, "rc": p.returncode,
                "output": (p.stdout or p.stderr or "")[:500]}
    except Exception as e:
        return {"notified": False, "reason": "notify command failed: %s" % e}


def notify_ntfy(hit, cfg, kind):
    """POST to ntfy.sh/<topic>. No account needed; works for any installer."""
    topic = (cfg.get("notify_ntfy_topic") or "").strip().strip("/")
    if not topic:
        return {"notified": False, "reason": "no notify_ntfy_topic configured"}
    title = ("Table booked: %s" % hit["name"]) if kind == "booking" \
        else ("Table available: %s" % hit["name"])
    body = "%s party of %s — %s" % (hit["date"], hit["party_size"],
                                    hit.get("slot", "").split("T")[-1])
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic, data=body.encode("utf-8"), method="POST",
        headers={"Title": title, "Tags": "bell"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"notified": 200 <= r.status < 300, "status": r.status}
    except Exception as e:
        return {"notified": False, "reason": "ntfy failed: %s" % e}


def notify_webhook(hit, cfg, kind):
    """POST a JSON event to notify_webhook_url."""
    url = (cfg.get("notify_webhook_url") or "").strip()
    if not url:
        return {"notified": False, "reason": "no notify_webhook_url configured"}
    payload = {"event": "table_watcher.%s" % kind, "kind": kind,
               "restaurant": hit["name"], "rid": hit["rid"], "date": hit["date"],
               "time": hit.get("slot", ""), "party_size": hit["party_size"]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "table-watcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"notified": 200 <= r.status < 300, "status": r.status}
    except Exception as e:
        return {"notified": False, "reason": "webhook failed: %s" % e}


def notify_all(hit, cfg, kind="hit"):
    """Fan out across every configured channel. kind is 'hit' or 'booking'."""
    return {"command": notify_command(hit, cfg),
            "ntfy": notify_ntfy(hit, cfg, kind),
            "webhook": notify_webhook(hit, cfg, kind)}


# ------------------------------------------------------------------- modes ---

def cmd_status(cfg, state):
    queue = state.get("queue") or []
    pointer = int(state.get("pointer", 0)) % max(1, len(queue))
    pct = 100.0 * pointer / len(queue) if queue else 0.0
    summary = state.get("last_summary") or {}
    print("queue:      %d combos, pointer at %d (%.1f%% swept)" % (len(queue), pointer, pct))
    print("fingerprint:%s" % state.get("queue_fingerprint", "?"))
    print("last run:   %s" % (state.get("last_run") or "never"))
    print("last scan:  %s" % json.dumps(summary))
    rl = state.get("rate_limited_until") or ""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print("rate limit: %s" % ("ACTIVE until %s" % rl if rl and rl > now else "none"))
    print("far-out skips: %d dates" % len(state.get("far_out") or {}))
    hits = state.get("last_hits") or []
    if hits:
        print("last hits (%d):" % len(hits))
        for h in hits:
            print("  - %s %s party of %s%s" % (
                h["name"], h.get("slot", h["date"]), h["party_size"],
                " [BOOKED]" if (h.get("booking") or {}).get("confirmed") else ""))
    else:
        print("last hits:  none")
    return 0


def cmd_scout(cfg, state, state_path, log_dir):
    """Sweep the whole queue; write a Markdown availability report."""
    queue = state.get("queue") or []
    if not queue:
        log("queue is empty (check date_range / restaurant rids in config)")
        return 4
    gap = float(cfg.get("scout_call_gap_seconds",
                        cfg.get("call_gap_seconds", 8)))
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.timezone.utc)
    log("scout: sweeping %d combos (gap %.0fs) ..." % (len(queue), gap))

    per_combo = []
    rate_limited = False
    for n, combo in enumerate(queue):
        key = "%s_%s" % (combo["rid"], combo["date"])
        retry_after = (state.get("far_out") or {}).get(key)
        if retry_after and retry_after > today:
            per_combo.append({"combo": combo, "skipped": "far_out"})
            continue
        if n > 0:
            time.sleep(gap)
        if n % 25 == 0:
            log("  scout %d/%d ..." % (n, len(queue)))
        try:
            res = get_provider(combo.get("provider")).check(combo, cfg)
        except ValueError as e:
            log("  ERROR: %s" % e)
            return 4
        per_combo.append({"combo": combo, "result": res})
        if res.get("rate_limited"):
            rate_limited = True
            state["rate_limited_until"] = (
                now + datetime.timedelta(seconds=60)).isoformat()
            log("  RATE LIMITED — stopping scout early")
            break
        if res.get("far_out"):
            retry = (datetime.date.today()
                     + datetime.timedelta(days=int(cfg.get("far_out_retry_days", 7)))).isoformat()
            state.setdefault("far_out", {})[key] = retry

    # Aggregate: restaurant -> date -> party sizes with slots
    by_rest = {}
    far_out_dates = set()
    checked = 0
    for entry in per_combo:
        if "skipped" in entry:
            continue
        combo, res = entry["combo"], entry["result"]
        checked += 1
        rest = by_rest.setdefault(combo["name"], {"rid": combo["rid"], "dates": {}})
        if res.get("far_out"):
            far_out_dates.add("%s %s" % (combo["name"], combo["date"]))
            continue
        slots = res.get("in_window_slots") or []
        if slots:
            d = rest["dates"].setdefault(combo["date"], [])
            d.append({"party_size": combo["party_size"],
                      "times": sorted(s.split("T")[1] for s in slots)})

    total_open = sum(len(v["dates"]) for v in by_rest.values())
    lines = []
    lines.append("# Table scout — %s" % now.strftime("%Y-%m-%d"))
    lines.append("")
    lines.append("Window %s–%s, parties of %s. Swept %d combos%s."
                 % (cfg["time_window"]["start"], cfg["time_window"]["end"],
                    "/".join(str(s) for s in cfg["party_sizes"]),
                    checked, " (stopped early: rate limited)" if rate_limited else ""))
    lines.append("")
    if total_open == 0:
        lines.append("**No in-window tables found anywhere in range.**")
    else:
        lines.append("**%d date(s) with in-window tables:**" % total_open)
    lines.append("")
    for name in sorted(by_rest):
        info = by_rest[name]
        dates = info["dates"]
        lines.append("## %s" % name)
        if not dates:
            lines.append("- nothing in window")
        for ds in sorted(dates):
            parts = "; ".join("party of %s at %s" % (p["party_size"], ", ".join(p["times"]))
                              for p in sorted(dates[ds], key=lambda p: p["party_size"]))
            lines.append("- %s: %s" % (ds, parts))
        lines.append("")
    if far_out_dates:
        lines.append("Books not open yet for %d restaurant-date(s); skipped."
                     % len(far_out_dates))

    day = now.strftime("%Y-%m-%d")
    md_path = os.path.join(log_dir, "scout_%s.md" % day)
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    json_path = os.path.join(log_dir, "scout_%s.json" % day)
    with open(json_path, "w") as f:
        json.dump({"run_at": now.isoformat(), "combos_swept": checked,
                   "rate_limited": rate_limited,
                   "open_dates": {n: v["dates"] for n, v in by_rest.items()}},
                  f, indent=2)
    save_state(state_path, state)
    log("scout DONE: %d open date(s) across %d restaurant(s); report -> %s"
        % (total_open, len(by_rest), md_path))
    return 2 if rate_limited else 0


def cmd_cancel(args, cfg):
    if "--rid" not in args or "--confirmation-id" not in args:
        log("usage: watch.py --cancel --rid RID --confirmation-id ID")
        return 4
    rid = args[args.index("--rid") + 1]
    cid = args[args.index("--confirmation-id") + 1]
    rc, out, err, timed_out = run_cli(
        ["cancel-reservation", "--rid", str(rid), "--confirmation-id", cid],
        cfg.get("command_timeout_seconds", 25))
    text = (out + "\n" + err).strip()
    print(text[:2000] if text else "(empty response)")
    ok = rc == 0 and not timed_out and "cancel" in text.lower()
    print("CANCELLED" if ok else "NOT confirmed cancelled — check output above")
    return 0 if ok else 1


def run_batch(cfg, state, state_path, log_dir):
    now = datetime.datetime.now(datetime.timezone.utc)
    now_tag = now.strftime("%Y-%m-%dT%H%M%SZ")

    rl_until = state.get("rate_limited_until") or ""
    if rl_until and rl_until > now.isoformat():
        log("rate limit still in effect until %s; skipping run" % rl_until)
        return 2

    queue = state["queue"]
    if not queue:
        log("queue is empty (check date_range / restaurant rids in config)")
        return 4

    today = datetime.date.today().isoformat()
    batch_size = int(cfg.get("check_batch_size", 3))
    gap = float(cfg.get("call_gap_seconds", 8))
    far_out_retry = int(cfg.get("far_out_retry_days", 7))

    # Select one batch starting at the pointer, skipping far-out dates not yet due.
    batch, indices = [], []
    i = int(state.get("pointer", 0)) % len(queue)
    attempts = 0
    while len(batch) < batch_size and attempts < len(queue) * 2:
        if i >= len(queue):
            i = 0
        combo = queue[i]
        key = "%s_%s" % (combo["rid"], combo["date"])
        retry_after = (state.get("far_out") or {}).get(key)
        if retry_after and retry_after > today:
            i += 1
            attempts += 1
            continue
        batch.append(combo)
        indices.append(i)
        i += 1
        attempts += 1

    log("batch of %d (queue indices %s), pointer was %d" % (len(batch), indices, state.get("pointer", 0)))

    results, hits, bookings = [], [], []
    rate_limited = False
    for n, combo in enumerate(batch):
        if n > 0:
            time.sleep(gap)  # one provider call at a time, paced
        log("  checking %s %s party of %s ..." % (combo["name"], combo["date"], combo["party_size"]))
        try:
            res = get_provider(combo.get("provider")).check(combo, cfg)
        except ValueError as e:
            log("  ERROR: %s" % e)
            return 4
        results.append(res)
        if res["rate_limited"]:
            rate_limited = True
            retry_after = (now + datetime.timedelta(seconds=60)).isoformat()
            state["rate_limited_until"] = retry_after
            log("  RATE LIMITED — stopping run, will resume after %s" % retry_after)
            break
        if res["far_out"]:
            key = "%s_%s" % (combo["rid"], combo["date"])
            retry = (datetime.date.today()
                     + datetime.timedelta(days=far_out_retry)).isoformat()
            state.setdefault("far_out", {})[key] = retry
            log("  books not open that far out; retrying %s after %s" % (key, retry))
        if res["in_window_slots"]:
            slot = res["in_window_slots"][0]
            hit = {"rid": combo["rid"], "name": combo["name"], "date": combo["date"],
                   "party_size": combo["party_size"], "slot": slot,
                   "all_slots": res["in_window_slots"],
                   "provider": combo.get("provider", "opentable")}
            hits.append(hit)
            log("  HIT: %s %s party of %s" % (combo["name"], slot, combo["party_size"]))
        else:
            log("  no in-window slots (reasons: %s)" % (res["reasons"] or "none listed"))

    # Book at most ONE table per run: the highest-priority hit (restaurant
    # priority, then earliest date). Lower-priority hits only notify.
    if hits:
        prio = {r["rid"]: r.get("priority", 99) for r in cfg["restaurants"]}
        hits.sort(key=lambda h: (prio.get(h["rid"], 99), h["date"], h["slot"]))
        if cfg.get("auto_book"):
            best = hits[0]
            log("  auto_book=true — booking best hit only: %s %s party of %s (explicit opt-in)"
                % (best["name"], best["slot"], best["party_size"]))
            time.sleep(gap)
            b = get_provider(best.get("provider")).book(
                {"rid": best["rid"], "party_size": best["party_size"]},
                best["slot"], cfg)
            b.update({"name": best["name"], "date": best["date"],
                      "party_size": best["party_size"], "slot": best["slot"]})
            bookings.append(b)
            best["booking"] = {"confirmed": b["confirmed"]}
            log("  booking %s" % ("CONFIRMED" if b["confirmed"]
                                  else "NOT confirmed: %s" % b.get("output_preview", "")[:200]))
            if b["confirmed"]:
                best["notify"] = notify_all(best, cfg, kind="booking")
            else:
                log("  booking failed — will NOT retry automatically; notifying instead")
                best["notify"] = notify_all(best, cfg, kind="hit")
            for other in hits[1:]:
                other["notify"] = notify_all(other, cfg, kind="hit")
                log("  (lower-priority hit not booked: %s %s)" % (other["name"], other["slot"]))
        else:
            for hit in hits:
                hit["notify"] = notify_all(hit, cfg, kind="hit")
            log("  notify-only mode: %d hit(s)" % len(hits))

    state["pointer"] = i % len(queue)
    state["last_run"] = now.isoformat()
    state["last_hits"] = hits
    state["last_summary"] = {"scanned": len(results), "hits": len(hits),
                             "bookings_attempted": len(bookings),
                             "bookings_confirmed": sum(1 for b in bookings if b.get("confirmed")),
                             "rate_limited": rate_limited}
    save_state(state_path, state)

    log_path = os.path.join(log_dir, "scan_%s.jsonl" % now_tag)
    with open(log_path, "w") as lf:
        for r in results:
            lf.write(json.dumps(r) + "\n")
    summary = {"run_at": now.isoformat(), "batch_indices": indices,
               "results": results, "hits": hits, "bookings": bookings,
               "pointer_after": state["pointer"], "rate_limited": rate_limited}
    with open(os.path.join(log_dir, "scan_%s_summary.json" % now_tag), "w") as sf:
        json.dump(summary, sf, indent=2)

    log("DONE: scanned %d, hits %d, bookings confirmed %d, pointer -> %d"
        % (len(results), len(hits),
           sum(1 for b in bookings if b.get("confirmed")), state["pointer"]))
    return 2 if rate_limited else 0


def main():
    args = sys.argv[1:]
    config_path = DEFAULT_CONFIG
    if "--config" in args:
        idx = args.index("--config")
        if idx + 1 >= len(args):
            log("ERROR: --config requires a path argument")
            sys.exit(4)
        config_path = args[idx + 1]
    if not os.path.exists(config_path):
        log("ERROR: config not found at %s\nCopy scripts/config.example.json to scripts/config.json and fill it in." % config_path)
        sys.exit(4)
    with open(config_path) as f:
        cfg = json.load(f)

    repo_root = os.path.dirname(SCRIPT_DIR)
    state_dir = cfg.get("state_dir") or os.path.join(repo_root, "state")
    log_dir = cfg.get("log_dir") or os.path.join(repo_root, "logs")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "scan_state.json")
    state = load_state(state_path, cfg)

    if "--status" in args:
        sys.exit(cmd_status(cfg, state))
    if "--scout" in args:
        sys.exit(cmd_scout(cfg, state, state_path, log_dir))
    if "--cancel" in args:
        sys.exit(cmd_cancel(args, cfg))
    sys.exit(run_batch(cfg, state, state_path, log_dir))


if __name__ == "__main__":
    main()
