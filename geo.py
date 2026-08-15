"""Geo helpers: haversine distance, multi-mode travel time, geocoding, and a
"value" score (rating vs travel effort).

Travel time is computed per mode, all free (no Google key needed):
  driving  -> OSRM car route (FOSSGIS/OpenStreetMap), or Google Distance Matrix if key set
  walking  -> OSRM foot route (FOSSGIS)
  bike     -> OSRM bike route (FOSSGIS)
  transit  -> OeBB Scotty HAFAS (covers Wiener Linien / VOR), free, no key

Only if a router fails do we fall back to a straight-line speed estimate.
Results are cached in SQLite (default 6h).
"""
import datetime
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from db import DB

EARTH_R_KM = 6371.0
CITY_SPEED_KMH = float(os.environ.get("TGTG_CITY_SPEED_KMH", "28"))
TRAVEL_TTL_S = int(os.environ.get("TGTG_TRAVEL_TTL_S", str(6 * 3600)))

# FOSSGIS hosts real car/foot/bike OSRM profiles (the project-osrm.org demo only
# serves the car profile, which is why foot/bike must come from here).
OSRM_BASE = "https://routing.openstreetmap.de"
OSRM_PROFILES = {"driving": "car", "walking": "foot", "bike": "bike"}

# fallback speeds (km/h) for the straight-line estimate when no router answers
TRAVEL_SPEED_KMH = {"driving": CITY_SPEED_KMH, "walking": 4.8, "bike": 15.0,
                    "transit": 22.0}
TRAVEL_LABELS = {"driving": "🚗", "transit": "🚌", "walking": "🚶", "bike": "🚲"}

# OeBB Scotty HAFAS (public, free, no key) — profile values from hafas-client
HAFAS_ENDPOINT = "https://fahrplan.oebb.at/bin/mgate.exe"
HAFAS_AUTH = {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"}
HAFAS_CLIENT = {"type": "IPH", "id": "OEBB", "v": "6030600", "name": "oebbPROD-ADHOC"}


def haversine_km(lat1, lon1, lat2, lon2):
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def estimate_minutes(km, travel="driving"):
    """Last-resort straight-line estimate for a given travel mode."""
    if km is None:
        return None
    m = km / TRAVEL_SPEED_KMH.get(travel, CITY_SPEED_KMH) * 60.0
    if travel == "transit":
        m += 5  # average waiting/headway
    return m


def _fmt_key(lat, lon):
    return f"{lat:.5f},{lon:.5f}"


def _parse_hhmmss(s):
    """HAFAS durations/times are 'HHMMSS' strings; return minutes."""
    if not s or len(s) < 6:
        return None
    try:
        return int(s[0:2]) * 60 + int(s[2:4]) + int(s[4:6]) / 60.0
    except ValueError:
        return None


class Geo:
    def __init__(self, db: DB):
        self.db = db
        self.key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

    def _get_json(self, url):
        req = urllib.request.Request(url, headers={"user-agent": "tgtg-bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def _post_json(self, url, body):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "user-agent": "tgtg-bot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    # ---- routing providers --------------------------------------------
    def _google_minutes(self, olat, olon, dlat, dlon):
        url = ("https://maps.googleapis.com/maps/api/distancematrix/json"
               "?units=metric&mode=driving"
               f"&origins={olat},{olon}&destinations={dlat},{dlon}&key={self.key}")
        data = self._get_json(url)
        elem = (data.get("rows") or [{}])[0].get("elements") or [{}]
        elem = elem[0]
        if elem.get("status") != "OK":
            return None
        return elem["duration"]["value"] / 60.0

    def _osrm_minutes(self, olat, olon, dlat, dlon, profile):
        # FOSSGIS URL shape: /routed-{car|foot|bike}/route/v1/driving/{lon,lat;...}
        # OSRM wants coordinates as lon,lat
        url = (f"{OSRM_BASE}/routed-{profile}/route/v1/driving/"
               f"{olon},{olat};{dlon},{dlat}?overview=false")
        data = self._get_json(url)
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        return data["routes"][0]["duration"] / 60.0

    def _hafas_minutes(self, olat, olon, dlat, dlon):
        now = datetime.datetime.now() + datetime.timedelta(minutes=2)
        body = {
            "ver": "1.45", "lang": "de", "id": "1|tgtg-bot",
            "auth": HAFAS_AUTH, "client": HAFAS_CLIENT,
            "svcReqL": [{
                "cfg": {"polyEnc": "GPA"},
                "meth": "TripSearch",
                "req": {
                    "depLocL": [{"type": "C", "crd": {"x": int(lon * 1e6),
                                                      "y": int(lat * 1e6)}}
                                for lat, lon in ((olat, olon),)],
                    "arrLocL": [{"type": "C", "crd": {"x": int(lon * 1e6),
                                                      "y": int(lat * 1e6)}}
                                for lat, lon in ((dlat, dlon),)],
                    "outDate": now.strftime("%Y%m%d"),
                    "outTime": now.strftime("%H%M%S"),
                    "getPolyline": False, "getPasslist": False,
                    "getIST": False, "minChgTime": -1, "numF": 1,
                },
            }],
        }
        data = self._post_json(HAFAS_ENDPOINT, body)
        if data.get("err") != "OK":
            return None
        res = (data.get("svcResL") or [{}])[0].get("res") or {}
        cons = res.get("outConL") or []
        if not cons:
            return None
        return _parse_hhmmss(cons[0].get("dur"))

    # ---- public API ---------------------------------------------------
    def travel_minutes(self, olat, olon, dlat, dlon, travel="driving"):
        """(minutes, source) where source is 'google' | 'osrm' | 'hafas' | None.
        None means no router answered — caller falls back to estimate."""
        origin, dest = _fmt_key(olat, olon), _fmt_key(dlat, dlon)

        if travel == "driving" and self.key:
            cached = self.db.get_travel(origin, dest, "google:driving", TRAVEL_TTL_S)
            if cached is not None:
                return cached, "google"
            try:
                m = self._google_minutes(olat, olon, dlat, dlon)
                if m is not None:
                    self.db.set_travel(origin, dest, m, "google:driving")
                    return m, "google"
            except Exception:
                pass

        if travel in OSRM_PROFILES:
            mode_key = f"osrm:{travel}"
            cached = self.db.get_travel(origin, dest, mode_key, TRAVEL_TTL_S)
            if cached is not None:
                return cached, "osrm"
            try:
                m = self._osrm_minutes(olat, olon, dlat, dlon, OSRM_PROFILES[travel])
                if m is not None:
                    self.db.set_travel(origin, dest, m, mode_key)
                    return m, "osrm"
            except Exception:
                pass
            return None, None

        if travel == "transit":
            cached = self.db.get_travel(origin, dest, "hafas", TRAVEL_TTL_S)
            if cached is not None:
                return cached, "hafas"
            try:
                m = self._hafas_minutes(olat, olon, dlat, dlon)
                if m is not None:
                    self.db.set_travel(origin, dest, m, "hafas")
                    return m, "hafas"
            except Exception:
                pass
            return None, None

        return None, None

    # ---- geocoding ----------------------------------------------------
    def geocode(self, text):
        """Address text -> (lat, lng) or None.
        Google first (if a key is set), then free Nominatim (OpenStreetMap)."""
        if self.key:
            r = self._geocode_google(text)
            if r:
                return r
        return self._geocode_nominatim(text)

    def _geocode_google(self, text):
        url = ("https://maps.googleapis.com/maps/api/geocode/json"
               f"?address={urllib.parse.quote(text)}&key={self.key}")
        try:
            data = self._get_json(url)
            if data.get("status") != "OK" or not data.get("results"):
                return None
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        except Exception:
            return None

    def _geocode_nominatim(self, text):
        """Free OSM geocoder; usage policy requires <=1 req/s and a UA, so we throttle."""
        now = time.time()
        if now - getattr(self, "_last_geo", 0) < 1.1:
            time.sleep(1.1 - (now - self._last_geo))
        self._last_geo = time.time()
        url = ("https://nominatim.openstreetmap.org/search"
               f"?format=json&limit=1&addressdetails=0&q={urllib.parse.quote(text)}")
        try:
            data = self._get_json(url)
            if not data:
                return None
            return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception:
            return None


def value_score(rating, km=None, minutes=None):
    """Rating-per-effort: which store is best for the travel cost.
    Uses time when available, otherwise distance."""
    if rating is None:
        return None
    if minutes is not None:
        return rating / max(minutes, 3.0)
    if km is not None:
        return rating / max(km, 0.5)
    return None
