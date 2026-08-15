"""Wiener Linien OGD Echtzeitdaten client — real-time departure board.

Free, no key. Sources:
  - stops:   wl_stops.json (built from the 'haltepunkte' CSV by build_stops.py)
  - realtime: GET https://www.wienerlinien.at/ogd_realtime/monitor?rbl=<id>&activateTrafficInfo=vo

Each RBL id is (stop + line + direction), so a station has several ids; we
merge them to show all lines at a stop. Fair use: >=15s between polls.
"""
import concurrent.futures
import json
import os
import urllib.request

from geo import haversine_km

STOPS_PATH = os.path.join(os.path.dirname(__file__), "wl_stops.json")
MONITOR_URL = "https://www.wienerlinien.at/ogd_realtime/monitor"

_stops = None


def load_stops():
    global _stops
    if _stops is None:
        with open(STOPS_PATH) as f:
            _stops = json.load(f)
    return _stops


def find_stops(name, limit=6):
    q = (name or "").strip().lower()
    stops = load_stops()
    if not q:
        return []
    hits = [s for s in stops if q in s["n"].lower()]
    hits.sort(key=lambda s: (not s["n"].lower().startswith(q), s["n"].lower()))
    return hits[:limit]


def nearest_stop(lat, lon):
    if lat is None or lon is None:
        return None
    stops = load_stops()
    best = min(stops, key=lambda s: haversine_km(lat, lon, s["lat"], s["lon"]) or 1e9)
    return best


def _departures_for_rbl(rbl):
    url = f"{MONITOR_URL}?rbl={rbl}&activateTrafficInfo=vo"
    req = urllib.request.Request(url, headers={"user-agent": "tgtg-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    m = (data.get("data") or {}).get("monitors") or [{}]
    m = m[0]
    out = []
    for ln in m.get("lines") or []:
        for dep in (ln.get("departures") or {}).get("departure") or []:
            t = dep.get("departureTime") or {}
            out.append({
                "line": ln.get("name") or "?",
                "towards": ln.get("towards") or "",
                "countdown": t.get("countdown"),
                "realtime": bool(t.get("realTime")),
            })
    return out


def station_departures(stop, max_rbls=12, limit=10):
    """Merged next departures for a whole station (all lines/directions)."""
    merged = {}
    for rbl in stop["r"][:max_rbls]:
        try:
            for d in _departures_for_rbl(rbl):
                c = d["countdown"]
                if c is None:
                    continue
                c = int(c)
                key = (d["line"], d["towards"])
                prev = merged.get(key)
                if prev is None or c < prev["countdown"]:
                    merged[key] = {"line": d["line"], "towards": d["towards"],
                                   "countdown": c, "realtime": d["realtime"]}
        except Exception:
            continue
    out = sorted(merged.values(), key=lambda d: d["countdown"])
    return out[:limit]


def departures_parallel(stop, max_rbls=12, limit=10):
    """Fetch each RBL in a small thread pool so big stations stay fast."""
    rbls = stop["r"][:max_rbls]
    merged = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(_safe_rbl, rbls):
            for d in res:
                c = d["countdown"]
                if c is None:
                    continue
                c = int(c)
                key = (d["line"], d["towards"])
                prev = merged.get(key)
                if prev is None or c < prev["countdown"]:
                    merged[key] = {"line": d["line"], "towards": d["towards"],
                                   "countdown": c, "realtime": d["realtime"]}
    return sorted(merged.values(), key=lambda d: d["countdown"])[:limit]


def _safe_rbl(rbl):
    try:
        return _departures_for_rbl(rbl)
    except Exception:
        return []
