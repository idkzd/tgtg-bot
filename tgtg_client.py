"""TGTG API client — talks to api.toogoodtogo.com with the session captured
from the real Android app (access/refresh token + rotating datadome cookie).

Endpoints used (reverse-engineered from app 26.8.0):
  POST /api/token/v1/refresh           {"refresh_token": ...}
  POST /api/itemmap/v2/clusters        map bounding-box query, full item detail
  POST /api/discover/v1/               feed (not used here, kept for reference)
"""
import base64
import json
import math
import os
import ssl
import time
import urllib.request
import urllib.error
import uuid

API = "https://api.toogoodtogo.com"
UA = "TGTG/26.8.0 Dalvik/2.1.0 (Linux; U; Android 14; sdk_gphone64_x86_64 Build/UE1A.230829.050)"

# Headers the app sends on every call. Timezone offset is device-local (+02:00 Vienna);
# set via TGTG_TIMEZONE_OFFSET if the account is used from another zone.
HEADERS_BASE = {
    "user-agent": UA,
    "x-timezoneoffset": os.environ.get("TGTG_TIMEZONE_OFFSET", "+02:00"),
    "x-24hourformat": os.environ.get("TGTG_24HOUR", "false"),
    "accept-language": "en-US",
    "content-type": "application/json; charset=utf-8",
}

DEFAULT_SESSION = os.environ.get("TGTG_SESSION_PATH") or os.path.join(
    os.path.dirname(__file__), "session.json")


class TgtgError(Exception):
    pass


class TgtgClient:
    def __init__(self, session_path=DEFAULT_SESSION, proxy=None):
        self.session_path = session_path
        self.proxy = proxy or os.environ.get("TGTG_PROXY")  # e.g. http://127.0.0.1:8080
        self._refreshing = False      # guard against recursive 401→refresh→401 loops
        self._dd_retrying = False     # guard against recursive datadome retries
        self._load()

    # ---- session persistence -------------------------------------------
    def _load(self):
        # On hosts without a writable/persistent file (Render/Koyeb) the session
        # comes from an env var; we still write it back to the file so token
        # refreshes persist for the lifetime of the container.
        env_json = os.environ.get("TGTG_SESSION_JSON")
        if env_json:
            try:
                self.session = json.loads(env_json)
            except json.JSONDecodeError as e:
                raise TgtgError(f"TGTG_SESSION_JSON is not valid JSON: {e}")
            try:
                self._save()
            except OSError:
                pass
            return
        try:
            with open(self.session_path) as f:
                self.session = json.load(f)
        except FileNotFoundError:
            raise TgtgError(
                f"No session found: neither TGTG_SESSION_JSON env var nor "
                f"{self.session_path} exists. Copy session.json contents to "
                f"TGTG_SESSION_JSON env var on Render."
            )
        except json.JSONDecodeError as e:
            raise TgtgError(f"{self.session_path} is not valid JSON: {e}")

    def _save(self):
        tmp = self.session_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.session, f, indent=2)
        os.replace(tmp, self.session_path)

    @property
    def access_token(self):
        return self.session.get("access_token")

    @property
    def _datadome(self):
        return self.session.get("datadome") or ""

    def _set_datadome(self, value):
        if value:
            self.session["datadome"] = value
            self._save()

    def _jwt_exp(self, token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return int(data.get("exp", 0))
        except Exception:
            return 0

    # ---- low-level HTTP ------------------------------------------------
    def _post(self, path, body):
        url = API + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in HEADERS_BASE.items():
            req.add_header(k, v)
        req.add_header("x-correlation-id", str(uuid.uuid4()))
        if self._datadome:
            req.add_header("cookie", "datadome=" + self._datadome)
        if self.access_token:
            req.add_header("authorization", "Bearer " + self.access_token)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"https": self.proxy, "http": self.proxy}))
        opener = urllib.request.build_opener(*handlers)

        try:
            with opener.open(req, timeout=40) as r:
                status = r.status
                raw = r.read()
                for k, v in r.headers.items():
                    if k.lower() == "set-cookie" and v.startswith("datadome="):
                        self._set_datadome(v.split(";")[0].split("datadome=", 1)[1])
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
        text = raw.decode("utf-8", "replace")

        if status == 401 and not self._refreshing:
            # token expired mid-flight -> refresh once and retry
            if self.refresh():
                return self._post(path, body)
            raise TgtgError(f"401 on {path} and refresh failed")

        # Datadome blocks with 403 + HTML challenge when the cookie is
        # tied to a different IP. Drop it and retry — TGTG issues a fresh
        # one bound to the current IP.
        if status == 403 and not self._dd_retrying:
            print(f"[tgtg] Datadome 403 on {path}, dropping cookie and retrying without it…", flush=True)
            self.session.pop("datadome", None)
            try:
                self._save()
            except OSError:
                pass
            self._dd_retrying = True
            try:
                print(f"[tgtg] Retry {path} without datadome…", flush=True)
                result = self._post(path, body)
                print(f"[tgtg] Retry SUCCESS on {path}", flush=True)
                return result
            except Exception as e:
                print(f"[tgtg] Retry FAILED on {path}: {e}", flush=True)
                raise
            finally:
                self._dd_retrying = False

        if status not in (200, 202):
            raise TgtgError(f"{path} -> HTTP {status}: {text[:300]}")

        # Parse JSON; if it fails (Datadome served an HTML challenge page),
        # clear the stale cookie and retry once.
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            if self._dd_retrying or not self._datadome:
                raise TgtgError(
                    f"TGTG returned non-JSON response (likely Datadome block). "
                    f"First 120 chars: {text[:120]}")
            self.session.pop("datadome", None)
            try:
                self._save()
            except OSError:
                pass
            self._dd_retrying = True
            try:
                return self._post(path, body)
            finally:
                self._dd_retrying = False

    # ---- auth ----------------------------------------------------------
    def refresh(self):
        """POST /api/token/v1/refresh, update both tokens. Returns True on success."""
        rt = self.session.get("refresh_token")
        if not rt:
            return False
        # Prevent recursive refresh: if _post gets a 401 on the refresh call
        # itself, don't try to refresh again (that would loop forever).
        self._refreshing = True
        try:
            out = self._post("/api/token/v1/refresh", {"refresh_token": rt})
        except TgtgError:
            return False
        finally:
            self._refreshing = False
        if not out.get("access_token"):
            return False
        self.session["access_token"] = out["access_token"]
        self.session["refresh_token"] = out.get("refresh_token") or rt
        self.session["access_token_ttl_seconds"] = out.get("access_token_ttl_seconds", 172800)
        self._save()
        return True

    def ensure_token(self):
        exp = self._jwt_exp(self.access_token or "")
        if exp and exp - time.time() < 3600:  # refresh with 1h margin
            self.refresh()

    # ---- items ---------------------------------------------------------
    def items_around(self, lat, lon, radius_km, with_stock_only=True):
        """Query itemmap/v2/clusters for a square box and return items within the
        requested radius (the API returns a bounding box, so we re-filter by distance)."""
        self.ensure_token()
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
        body = {
            "center_map_coordinate": {"latitude": lat, "longitude": lon},
            "origin": {"latitude": lat, "longitude": lon},
            "bounding_box": {
                "top_left": {"latitude": lat + dlat, "longitude": lon - dlon},
                "bottom_right": {"latitude": lat - dlat, "longitude": lon + dlon},
            },
            "item_detail_level": "FULL",
            "filtered_by": {"item_categories": [], "diet_categories": [],
                            "with_stock_only": with_stock_only},
            "sort_option": "RELEVANCE",
        }
        data = self._post("/api/itemmap/v2/clusters", body)
        out = []
        for entry in data.get("full_detail_items", []):
            item = entry.get("item", {}) or {}
            store = entry.get("store", {}) or {}
            r = item.get("average_overall_rating") or {}
            dist_km = entry.get("distance") or 0  # API returns distance in km
            if dist_km > radius_km + 0.01:  # slack for float rounding
                continue
            price = item.get("item_price") or {}
            price_val = None
            if price:
                price_val = price.get("minor_units", 0) / (10 ** price.get("decimals", 2))
            addr = (((store.get("store_location") or {}).get("address") or {})
                    .get("address_line") or "").strip()
            loc = (store.get("store_location") or {}).get("location") or {}
            out.append({
                "store_id": store.get("store_id"),
                "item_id": item.get("item_id"),
                "store_name": store.get("store_name") or "",
                "branch": store.get("branch") or "",
                "item_name": item.get("name") or "",
                "address": addr,
                "lat": loc.get("latitude"),
                "lng": loc.get("longitude"),
                "rating": r.get("average_overall_rating"),
                "rating_count": r.get("rating_count"),
                "distance_km": round(dist_km, 2),
                "in_stock": bool(entry.get("in_sales_window")),
                "price": price_val,
                "price_currency": price.get("code", "EUR"),
            })
        return out

    def top_stores(self, lat, lon, radius_km, sort_by="rating", min_reviews=0,
                   limit=10, with_stock_only=True):
        items = self.items_around(lat, lon, radius_km, with_stock_only)
        # collapse multiple bags of the same store to its best-rated entry
        best = {}
        for it in items:
            key = it["store_id"]
            cur = best.get(key)
            if cur is None or (it["rating"] or 0) > (cur["rating"] or 0):
                best[key] = it
        items = list(best.values())
        if min_reviews > 0:
            items = [i for i in items if (i["rating_count"] or 0) >= min_reviews]
        if sort_by == "rating":
            items.sort(key=lambda i: (-(i["rating"] or 0), -(i["rating_count"] or 0)))
        elif sort_by == "reviews":
            items.sort(key=lambda i: (-(i["rating_count"] or 0), -(i["rating"] or 0)))
        elif sort_by == "distance":
            items.sort(key=lambda i: i["distance_km"])
        return items[:limit]


    # ---- whole-city scan (grid) ---------------------------------------
    VIENNA = {"lat0": 48.12, "lat1": 48.33, "lon0": 16.16, "lon1": 16.59,
              "dlat": 0.045, "dlon": 0.035}

    @staticmethod
    def _normalize(entry):
        """Map a raw itemmap cluster entry to the flat dict we store in SQLite."""
        item = entry.get("item") or {}
        store = entry.get("store") or {}
        loc = store.get("store_location") or {}
        l = loc.get("location") or {}
        addr = (loc.get("address") or {}).get("address_line") or ""
        r = item.get("average_overall_rating") or {}
        price = item.get("item_price") or {}
        price_val = None
        if price:
            price_val = price.get("minor_units", 0) / (10 ** price.get("decimals", 2))
        return {
            "item_id": str(item.get("item_id") or ""),
            "store_id": str(store.get("store_id") or ""),
            "store_name": store.get("store_name") or "",
            "branch": store.get("branch") or "",
            "item_name": item.get("name") or "",
            "address": addr.strip(),
            "lat": l.get("latitude"),
            "lng": l.get("longitude"),
            "price": price_val,
            "currency": price.get("code", "EUR") if price else "EUR",
            "rating": r.get("average_overall_rating"),
            "rating_count": r.get("rating_count"),
            "in_sales_window": bool(entry.get("in_sales_window")),
        }

    def _tile_body(self, la, lo, dlat, dlon):
        return {
            "center_map_coordinate": {"latitude": la + dlat / 2, "longitude": lo + dlon / 2},
            "origin": {"latitude": la + dlat / 2, "longitude": lo + dlon / 2},
            "bounding_box": {
                "top_left": {"latitude": la + dlat, "longitude": lo},
                "bottom_right": {"latitude": la, "longitude": lo + dlon},
            },
            "item_detail_level": "FULL",
            "filtered_by": {"item_categories": [], "diet_categories": [],
                            "with_stock_only": False},
            "sort_option": "RELEVANCE",
        }

    def scan_vienna(self, progress=None, pause=1.2):
        """Walk Vienna with the map-tile grid; returns normalized entries.
        progress(step, total, found) is called after each tile. The server caps
        one response at ~1000 detail items, so a grid (65 tiles here) covers
        the whole city. """
        self.ensure_token()
        g = self.VIENNA
        tiles = []
        la = g["lat0"]
        while la < g["lat1"]:
            lo = g["lon0"]
            while lo < g["lon1"]:
                tiles.append((la, lo))
                lo += g["dlon"]
            la += g["dlat"]
        out = []
        blocked = 0
        for i, (la, lo) in enumerate(tiles):
            try:
                data = self._post("/api/itemmap/v2/clusters",
                                  self._tile_body(la, lo, g["dlat"], g["dlon"]))
            except TgtgError:
                # transient block (403 interstitial) — back off, keep going
                blocked += 1
                if blocked >= 5:
                    break
                time.sleep(4)
                continue
            blocked = 0
            for e in data.get("full_detail_items", []):
                n = self._normalize(e)
                if n["item_id"]:
                    out.append(n)
            if progress:
                progress(i + 1, len(tiles), len(out))
            time.sleep(pause)
        return out

    def item_status(self, item_id, lat, lng, radius_km=0.7):
        """Is this item currently in its sales window? One cheap box query.
        Returns True/False, or None if the store no longer offers this item."""
        try:
            items = self.items_around(lat, lng, radius_km, with_stock_only=False)
        except TgtgError:
            return None
        for it in items:
            if str(it.get("item_id")) == str(item_id):
                return bool(it.get("in_stock"))
        return None


def fmt_item(it):
    """Human-readable one-liner for a store."""
    rating = f"{it['rating']:.1f}" if it["rating"] is not None else "—"
    revs = it["rating_count"] if it["rating_count"] is not None else "—"
    status = "✅ есть" if it["in_stock"] else "⛔ распродано"
    name = it["store_name"]
    if it["branch"] and it["branch"] not in name:
        name += f" — {it['branch']}"
    price = ""
    if it["price"] is not None:
        price = f" · {it['price']:.2f} {it['price_currency']}"
    return (f"⭐ {rating} ({revs} отз.) · {it['distance_km']} км · {status}{price}\n"
            f"   {name}\n"
            f"   {it['item_name']}\n"
            f"   📍 {it['address']}")
