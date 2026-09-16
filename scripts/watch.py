#!/usr/bin/env python3
"""
table-watcher: poll OpenTable availability for hard-to-get restaurants.

One invocation = one small sequential batch of availability checks (cron-friendly).
Reads scripts/config.json, keeps a rotating pointer/queue in the state dir so
each run covers a different slice of the (restaurant x date x party-size) space,
writes per-run JSONL logs, and on a hit either notifies (default) or books
(auto_book=true, explicit opt-in only).

Stdlib only. Makes OpenTable CLI calls strictly one at a time with a pause
between them, and stops the whole run on any rate-limit signal (HTTP 429).
"""
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

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


def check_availability(combo, cfg):
    """One availability check. Returns a result dict."""
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
    result = {"rid": combo["rid"], "name": combo["name"], "date": combo["date"],
              "party_size": combo["party_size"], "rc": rc,
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


def try_book(combo, slot_iso, cfg):
    """Attempt one booking. Returns dict; only 'confirmed' counts as success."""
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
    # NOTE: verify exact flags with `opentable book-reservation --help`; the CLI
    # contract lives in the opentable skill. Do not invent a confirmation.
    rc, out, err, timed_out = run_cli(cmd, cfg.get("command_timeout_seconds", 25))
    text = (out + "\n" + err)
    low = text.lower()
    confirmed = (rc == 0 and not timed_out
                 and ("confirm" in low or "reservation" in low))
    return {"confirmed": confirmed, "rc": rc, "timed_out": timed_out,
            "output_preview": text.strip()[:1500]}


def notify(hit, cfg):
    """Run the configured notify command with hit details substituted."""
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


def main():
    args = sys.argv[1:]
    if "--config" in args:
        idx = args.index("--config")
        if idx + 1 >= len(args):
            log("ERROR: --config requires a path argument")
            sys.exit(4)
        config_path = args[idx + 1]
    else:
        config_path = DEFAULT_CONFIG
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

    now = datetime.datetime.now(datetime.timezone.utc)
    now_tag = now.strftime("%Y-%m-%dT%H%M%SZ")

    state = load_state(state_path, cfg)

    rl_until = state.get("rate_limited_until") or ""
    if rl_until and rl_until > now.isoformat():
        log("rate limit still in effect until %s; skipping run" % rl_until)
        sys.exit(2)

    queue = state["queue"]
    if not queue:
        log("queue is empty (check date_range / restaurant rids in config)")
        sys.exit(4)

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
            time.sleep(gap)  # one OpenTable call at a time, paced
        log("  checking %s %s party of %s ..." % (combo["name"], combo["date"], combo["party_size"]))
        res = check_availability(combo, cfg)
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
                   "all_slots": res["in_window_slots"]}
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
            b = try_book({"rid": best["rid"], "party_size": best["party_size"]},
                         best["slot"], cfg)
            b.update({"name": best["name"], "date": best["date"],
                      "party_size": best["party_size"], "slot": best["slot"]})
            bookings.append(b)
            best["booking"] = {"confirmed": b["confirmed"]}
            log("  booking %s" % ("CONFIRMED" if b["confirmed"]
                                  else "NOT confirmed: %s" % b.get("output_preview", "")[:200]))
            if not b["confirmed"]:
                log("  booking failed — will NOT retry automatically; notifying instead")
                best["notify"] = notify(best, cfg)
            for other in hits[1:]:
                other["notify"] = notify(other, cfg)
                log("  (lower-priority hit not booked: %s %s)" % (other["name"], other["slot"]))
        else:
            for hit in hits:
                hit["notify"] = notify(hit, cfg)
            log("  notify-only mode: %d hit(s)" % len(hits))

    state["pointer"] = i % len(queue)
    state["last_run"] = now.isoformat()
    state["last_hits"] = hits
    state["last_summary"] = {"scanned": len(results), "hits": len(hits),
                             "bookings_attempted": len(bookings),
                             "bookings_confirmed": sum(1 for b in bookings if b.get("confirmed")),
                             "rate_limited": rate_limited}
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

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
    sys.exit(2 if rate_limited else 0)


if __name__ == "__main__":
    main()
