"""Build wl_stops.json from Wiener Linien OGD 'haltepunkte' CSV.

Source: https://www.wienerlinien.at/ogd_realtime/doku/ogd/wienerlinien-ogd-haltepunkte.csv
Columns: StopID;DIVA;StopText;Municipality;MunicipalityID;Longitude;Latitude

Groups stops by DIVA (station) so one station has one name, one coordinate
and the list of its RBL ids (each RBL = stop + line + direction for the
realtime monitor API).
"""
import csv
import io
import json
import os
import urllib.request

CSV_URL = ("https://www.wienerlinien.at/ogd_realtime/doku/ogd/"
           "wienerlinien-ogd-haltepunkte.csv")
OUT = os.path.join(os.path.dirname(__file__), "wl_stops.json")


def norm_name(s):
    s = (s or "").strip()
    if s.endswith(" U"):
        s = s[:-2].strip()
    return s


def main():
    req = urllib.request.Request(CSV_URL, headers={"user-agent": "tgtg-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    stations = {}
    for row in csv.reader(io.StringIO(raw), delimiter=";"):
        if len(row) < 7:
            continue
        stop_id, diva, text, _mun, _mid, lon, lat = row[:7]
        if not diva or not stop_id:
            continue
        try:
            lon_f, lat_f = float(lon), float(lat)
        except ValueError:
            continue
        if lon_f == 0 or lat_f == 0:
            continue
        st = stations.setdefault(diva, {"n": "", "lat": 0.0, "lon": 0.0, "r": []})
        st["r"].append(int(stop_id))
        # keep the longest name variant (e.g. "Rochusgasse" over "Rochusgasse U")
        if len(norm_name(text)) >= len(st["n"]):
            st["n"] = norm_name(text)
        st["lat"] = lat_f
        st["lon"] = lon_f
    out = [{"d": d, "n": v["n"], "lat": v["lat"], "lon": v["lon"],
            "r": sorted(set(v["r"]))} for d, v in stations.items() if v["n"]]
    out.sort(key=lambda s: s["n"].lower())
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"built {OUT}: {len(out)} stations")


if __name__ == "__main__":
    main()
