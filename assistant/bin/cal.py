#!/usr/bin/env python3
"""Read-only Google Calendar (or any iCal feed) reader for the eTL assistant.

Reads the secret iCal URLs in ~/.config/etl-assistant/calendars.json:
  [{"name": "개인", "url": "https://calendar.google.com/calendar/ical/.../basic.ics"}]

Usage:
  cal.py                     today's events (KST)
  cal.py --days 7            the next 7 days, starting today
  cal.py --from 2026-11-20 --days 3

Prints JSON: [{calendar, title, start, end, all_day, location}], sorted by start.
Supports RRULE FREQ=DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, BYDAY, COUNT, UNTIL,
plus EXDATE and RECURRENCE-ID overrides, which covers what Google exports.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CONFIG = os.path.expanduser("~/.config/etl-assistant/calendars.json")
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 5000


def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def parse_prop(line):
    name_params, _, value = line.partition(":")
    parts = name_params.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_dt(value, params, default_tz):
    """Returns (datetime-or-date, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date(), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    try:
        tz = ZoneInfo(tzid) if tzid else default_tz
    except Exception:  # noqa: BLE001 - unknown TZID
        tz = default_tz
    return dt.replace(tzinfo=tz), False


def parse_events(text):
    events, cur, default_tz, depth = [], None, KST, []
    for line in unfold(text):
        if not line:
            continue
        name, params, value = parse_prop(line)
        if name == "BEGIN":
            depth.append(value.upper())
            if value.upper() == "VEVENT":
                cur = {"exdates": set()}
            continue
        if name == "END":
            if depth:
                depth.pop()
            if value.upper() == "VEVENT" and cur is not None:
                if "start" in cur:
                    events.append(cur)
                cur = None
            continue
        if name == "X-WR-TIMEZONE" and cur is None:
            try:
                default_tz = ZoneInfo(value.strip())
            except Exception:  # noqa: BLE001
                pass
        if cur is None or depth[-1:] != ["VEVENT"]:
            continue
        if name == "DTSTART":
            cur["start"], cur["all_day"] = parse_dt(value, params, default_tz)
        elif name == "DTEND":
            cur["end"], _ = parse_dt(value, params, default_tz)
        elif name == "DURATION":
            cur["duration"] = parse_duration(value)
        elif name == "SUMMARY":
            cur["title"] = unescape(value)
        elif name == "LOCATION":
            cur["location"] = unescape(value)
        elif name == "UID":
            cur["uid"] = value
        elif name == "STATUS":
            cur["status"] = value.upper()
        elif name == "RRULE":
            cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
        elif name == "EXDATE":
            for v in value.split(","):
                cur["exdates"].add(key_of(parse_dt(v, params, default_tz)[0]))
        elif name == "RECURRENCE-ID":
            cur["recurrence_id"] = key_of(parse_dt(value, params, default_tz)[0])
    return events


def parse_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(0)
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def key_of(d):
    """A comparable key for an occurrence start (UTC instant, or a date)."""
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return d.strftime("%Y%m%d")


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    try:
        return d.replace(year=d.year + y, month=m + 1)
    except ValueError:
        return None  # e.g. the 31st in a short month: RFC 5545 skips it


def occurrences(ev, range_end):
    """Yields occurrence starts of `ev` up to range_end (a date)."""
    start = ev["start"]
    rule = ev.get("rrule")
    if not rule:
        yield start
        return
    freq = rule.get("FREQ", "").upper()
    interval = int(rule.get("INTERVAL", 1) or 1)
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        u, _ = parse_dt(rule["UNTIL"], {}, KST)
        until = u
    byday = [DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS]
    start_date = start.date() if isinstance(start, datetime) else start

    def at(d):
        return datetime.combine(d, start.timetz()) if isinstance(start, datetime) else d

    def past_until(o):
        if until is None:
            return False
        if isinstance(o, datetime) and isinstance(until, datetime):
            return o > until
        od = o.date() if isinstance(o, datetime) else o
        ud = until.date() if isinstance(until, datetime) else until
        return od > ud

    n = 0
    for i in range(MAX_OCCURRENCES):
        if freq == "DAILY":
            cands = [start_date + timedelta(days=i * interval)]
        elif freq == "WEEKLY":
            week0 = start_date - timedelta(days=start_date.weekday())
            wk = week0 + timedelta(weeks=i * interval)
            days = byday or [start_date.weekday()]
            cands = [wk + timedelta(days=d) for d in sorted(days)]
        elif freq == "MONTHLY":
            c = add_months(start_date, i * interval)
            cands = [c] if c else []
        elif freq == "YEARLY":
            c = add_months(start_date, 12 * i * interval)
            cands = [c] if c else []
        else:
            yield start
            return
        for c in cands:
            if c < start_date:
                continue
            o = at(c)
            if past_until(o) or (count is not None and n >= count):
                return
            n += 1
            if c > range_end:
                return
            yield o


def to_kst(d):
    if isinstance(d, datetime):
        return d.astimezone(KST)
    return d


def fmt(d):
    if isinstance(d, datetime):
        return d.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    return d.strftime("%Y-%m-%d")


def events_in_range(cal_name, text, first, last):
    evs = parse_events(text)
    overrides = {}
    for ev in evs:
        if "recurrence_id" in ev:
            overrides[(ev.get("uid"), ev["recurrence_id"])] = ev
    out = []
    for ev in evs:
        if "recurrence_id" in ev:
            occs = [ev["start"]]
        else:
            occs = occurrences(ev, last)
        for o in occs:
            k = key_of(o)
            if "recurrence_id" not in ev:
                if k in ev["exdates"] or (ev.get("uid"), k) in overrides:
                    continue
            if ev.get("status") == "CANCELLED":
                continue
            dur = (ev["end"] - ev["start"]) if "end" in ev else ev.get("duration") or (
                timedelta(days=1) if ev.get("all_day") else timedelta(0))
            end = o + dur
            od = to_kst(o).date() if isinstance(o, datetime) else o
            ed = to_kst(end).date() if isinstance(end, datetime) else end - timedelta(days=1)
            if ed < first or od > last:
                continue
            out.append({"calendar": cal_name, "title": ev.get("title", "(제목 없음)"),
                        "start": fmt(o), "end": fmt(end if isinstance(end, datetime) else end - timedelta(days=1)),
                        "all_day": bool(ev.get("all_day")), "location": ev.get("location") or None,
                        "_sort": fmt(o) if isinstance(o, datetime) else fmt(o) + " 00:00"})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (default today, KST)")
    ap.add_argument("--days", type=int, default=1, help="number of days (default 1)")
    ap.add_argument("--file", help="read one .ics file instead of the config (for testing)")
    a = ap.parse_args()
    first = date.fromisoformat(a.start) if a.start else datetime.now(KST).date()
    last = first + timedelta(days=max(1, min(a.days, 366)) - 1)

    sources = []
    if a.file:
        sources = [{"name": os.path.basename(a.file), "text": open(a.file, encoding="utf-8").read()}]
    else:
        try:
            cals = json.load(open(CONFIG, encoding="utf-8"))
        except FileNotFoundError:
            sys.exit(f"no calendars configured: {CONFIG}")
        for c in cals:
            if not str(c.get("url", "")).startswith("https://"):
                print(f"calendar '{c.get('name')}' has no iCal URL yet in {CONFIG}", file=sys.stderr)
                continue
            try:
                req = urllib.request.Request(c["url"], headers={"User-Agent": "ical-reader"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    sources.append({"name": c.get("name", "calendar"), "text": r.read().decode("utf-8", "replace")})
            except Exception as e:  # noqa: BLE001 - don't echo the secret URL
                print(f"{c.get('name', 'calendar')}: fetch failed ({type(e).__name__})", file=sys.stderr)
    out = []
    for s in sources:
        out += events_in_range(s["name"], s["text"], first, last)
    out.sort(key=lambda e: e["_sort"])
    for e in out:
        del e["_sort"]
    print(json.dumps({"from": first.isoformat(), "to": last.isoformat(), "events": out},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
