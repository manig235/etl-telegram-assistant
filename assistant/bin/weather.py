#!/usr/bin/env python3
"""Hourly weather + umbrella call for the eTL assistant (read-only, no API key).

Source: MET Norway Locationforecast 2.0 (api.met.no), which covers Korea.
Default place: SNU Gwanak campus. Override with --lat/--lon (and --place for the label).

Usage:
  weather.py                     today (KST), 06:00–24:00
  weather.py --tomorrow
  weather.py --date 2026-11-20
  weather.py --lat 37.48 --lon 126.95 --place 집

Prints JSON: {place, date, umbrella: yes|maybe|no, reason, temp_min, temp_max,
rain_total_mm, rain_hours, hours: [{time, temp, rain_mm, sky}]}.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={lat:.4f}&lon={lon:.4f}"
UA = "etl-assistant/1.0 (personal weather lookup)"  # met.no requires an identifying User-Agent
SNU = (37.4596, 126.9519)
WET = ("rain", "sleet", "snow", "shower")
FROM_HOUR, TO_HOUR = 6, 24  # the part of the day the user is out

SKY_KO = {"clearsky": "맑음", "fair": "대체로 맑음", "partlycloudy": "구름 조금", "cloudy": "흐림",
          "fog": "안개", "lightrain": "약한 비", "rain": "비", "heavyrain": "강한 비",
          "lightrainshowers": "약한 소나기", "rainshowers": "소나기", "heavyrainshowers": "강한 소나기",
          "lightsleet": "약한 진눈깨비", "sleet": "진눈깨비", "lightsnow": "약한 눈", "snow": "눈",
          "heavysnow": "폭설", "snowshowers": "눈 소나기", "rainandthunder": "뇌우"}


def sky(code):
    if not code:
        return None
    base = code.split("_")[0]  # drop _day/_night/_polartwilight
    for k in sorted(SKY_KO, key=len, reverse=True):
        if base == k:
            return SKY_KO[k]
    return SKY_KO.get(base.replace("and", ""), base)


def fetch(lat, lon):
    req = urllib.request.Request(URL.format(lat=lat, lon=lon), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        sys.exit(f"weather request failed ({e.code})")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"weather service unreachable ({type(e).__name__})")


def build(data, day, place):
    hours = []
    for t in data.get("properties", {}).get("timeseries", []):
        when = datetime.fromisoformat(t["time"].replace("Z", "+00:00")).astimezone(KST)
        if when.date() != day or not FROM_HOUR <= when.hour < TO_HOUR:
            continue
        d = t.get("data", {})
        inst = d.get("instant", {}).get("details", {})
        nxt = d.get("next_1_hours") or d.get("next_6_hours") or {}
        span = 1 if "next_1_hours" in d else 6
        code = (nxt.get("summary") or {}).get("symbol_code")
        hours.append({"time": f"{when:%H:%M}", "span_h": span,
                      "temp": inst.get("air_temperature"),
                      "rain_mm": (nxt.get("details") or {}).get("precipitation_amount"),
                      "sky": sky(code), "_wet": bool(code and any(w in code for w in WET))})
    if not hours:
        sys.exit(f"no forecast for {day} (met.no covers about 9 days ahead)")

    temps = [h["temp"] for h in hours if h["temp"] is not None]
    rain = [h for h in hours if (h["rain_mm"] or 0) > 0 or h["_wet"]]
    total = round(sum(h["rain_mm"] or 0 for h in hours), 1)
    peak = max((h["rain_mm"] or 0) / h["span_h"] for h in hours)
    if total >= 1.0 or peak >= 0.5:
        umbrella, reason = "yes", "비 예보"
    elif rain:
        umbrella, reason = "maybe", "약한 비 가능성"
    else:
        umbrella, reason = "no", "비 예보 없음"
    if rain:
        reason += f" ({rain[0]['time']}–{rain[-1]['time']}, 합계 {total}mm)"
    for h in hours:
        del h["_wet"]
    return {"place": place, "date": day.isoformat(), "umbrella": umbrella, "reason": reason,
            "temp_min": min(temps) if temps else None, "temp_max": max(temps) if temps else None,
            "rain_total_mm": total, "rain_hours": [h["time"] for h in rain],
            "source": "MET Norway (api.met.no)", "hours": hours}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD (KST)")
    ap.add_argument("--tomorrow", action="store_true")
    ap.add_argument("--lat", type=float, default=SNU[0])
    ap.add_argument("--lon", type=float, default=SNU[1])
    ap.add_argument("--place", default=None, help="label for the output")
    a = ap.parse_args()
    if not (-90 <= a.lat <= 90 and -180 <= a.lon <= 180):
        sys.exit("bad coordinates")
    today = datetime.now(KST).date()
    day = a.date or (today + timedelta(days=1) if a.tomorrow else today)
    place = a.place or ("서울대 관악캠퍼스" if (a.lat, a.lon) == SNU else f"{a.lat:.3f},{a.lon:.3f}")
    print(json.dumps(build(fetch(a.lat, a.lon), day, place), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
