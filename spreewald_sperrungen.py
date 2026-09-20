#!/usr/bin/env python3
"""
Spreewald-Sperrkarte
====================
Liest die Sperrungsseite des LBV Brandenburg, ermittelt, was HEUTE gesperrt/eingeschränkt
ist, und schreibt eine Karte (OpenStreetMap) nach docs/index.html.

Aufruf:
    python spreewald_sperrungen.py                 # heute, Live-Seite, Overpass erlaubt
    python spreewald_sperrungen.py --date 2026-09-21
    python spreewald_sperrungen.py --html tests/fixture.html --no-osm   # Test ohne Netz
"""
import argparse, json, math, os, re, sys, time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

URL = ("https://lbv.brandenburg.de/lbv/de/verkehr/binnenschifffahrt-und-haefen/"
       "schiffbare-landesgewaesser-im-spreewald/sperrungen-auf-schiffbaren-landesgewaessern-im-spreewald/")
# Mehrere öffentliche Overpass-Server: wird einer abgelehnt, probieren wir den nächsten.
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"]
# Kontaktangabe für die OSM-Server (Höflichkeit bei automatischen Abfragen). Steht NICHT im Code,
# sondern wird aus der Umgebungsvariable KONTAKT gelesen (bei GitHub: Secret). Fehlt sie, geht es trotzdem.
KONTAKT = os.environ.get("KONTAKT") or "kein-kontakt-angegeben"
HEADERS = {"User-Agent": f"Spreewald-Sperrkarte/1.0 ({KONTAKT})", "Accept": "*/*",
           "Referer": "https://github.com/"}
BBOX = "51.70,13.70,52.10,14.40"          # grobe Box um den gesamten Spreewald (S,W,N,O)
HERE = Path(__file__).parent
def berlin_now():
    """Aktuelle Zeit in Deutschland (GitHub-Rechner laufen in UTC)."""
    try:
        return datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        return datetime.now()


WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


# ----------------------------------------------------------------------------- 1. Seite lesen
def fetch_html(path=None):
    if path:
        return Path(path).read_text(encoding="utf-8")
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Spreewald-Sperrkarte/1.0 (intern)"})
    r.raise_for_status()
    r.encoding = r.apparent_encoding if not r.encoding else r.encoding
    return r.text


def parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    stand = None
    m = re.search(r"Stand\s+(\d{1,2}\.\d{1,2}\.\d{4})", soup.get_text(" "))
    if m:
        stand = m.group(1)
    rows, seen = [], set()
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text("\n", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 5 or cells[0].lower().startswith("gewässer"):
                continue
            gew, bereich, zeitraum, grund, hinweis = cells[:5]
            key = (gew, bereich, zeitraum)
            if key in seen:               # die Seite enthält die Tabelle doppelt
                continue
            seen.add(key)
            rows.append(dict(gewaesser=gew, bereich=bereich, zeitraum=zeitraum,
                             grund=grund, hinweis=hinweis.replace("\n", " ")))
    return rows, stand


# ----------------------------------------------------------------------------- 2. Zeit & Status
DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.\s*(\d{4})")


def parse_period(text):
    low = text.lower()
    dates = [date(int(y), int(m), int(d)) for d, m, y in DATE.findall(text)]
    if "sofort" in low:
        start, end = None, (dates[0] if dates and "widerruf" not in low else None)
    elif "widerruf" in low:
        start, end = (dates[0] if dates else None), None
    else:
        start = dates[0] if dates else None
        end = dates[1] if len(dates) > 1 else start
    weekdays = {0, 1, 2, 3, 4} if "montag bis freitag" in low else None
    return start, end, weekdays


def classify(gew, hinweis):
    g, h = gew.lower(), hinweis.lower()
    if g.startswith("oberspreewald"):
        return "gebiet"                                    # Gebietshinweis (z. B. Krautung)
    if "kann passiert werden" in h or "ist passierbar" in h:
        if "vollsperrung" not in h:
            return "eingeschraenkt"
    if "vollsperrung" in h or "nicht möglich" in h:
        return "gesperrt"
    if "einschränkung" in g:
        return "eingeschraenkt"
    if "sperrung" in g:
        return "gesperrt"
    return "eingeschraenkt"


def status_on(row, day, lookahead):
    start, end, wd = parse_period(row["zeitraum"])
    row["start"], row["ende"] = (start.isoformat() if start else None), (end.isoformat() if end else None)
    if start and day < start:
        return "bald" if (start - day).days <= lookahead else None
    if end and day > end:
        return None
    if wd and day.weekday() not in wd:
        return "ruht"                                       # z. B. nur Mo-Fr gesperrt
    return classify(row["gewaesser"], row["hinweis"])


# ----------------------------------------------------------------------------- 3. Geometrie
def hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742000 * math.asin(math.sqrt(a))


class Geo:
    """Overpass-Abfragen mit Datei-Cache (geo_cache.json) -> täglicher Lauf bleibt schnell."""

    def __init__(self, offline):
        self.path = HERE / "geo_cache.json"
        self.cache = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.offline = offline

    def query(self, ql):
        if ql in self.cache:
            return self.cache[ql]
        if self.offline:
            return []
        for url in OVERPASS_URLS:
            host = url.split("/")[2]
            try:
                js = self._post(url, ql)
            except Exception as e:                          # nächsten Server probieren
                print(f"  ! {host}: {e}", file=sys.stderr)
                continue
            if js.get("remark"):
                print(f"  ! Overpass-Hinweis: {js['remark']}", file=sys.stderr)
            elements = js.get("elements", [])
            if elements:                                    # leere Antworten NICHT dauerhaft merken
                self.cache[ql] = elements
                self.path.write_text(json.dumps(self.cache), encoding="utf-8")
            else:
                print(f"  ? keine Treffer: {ql[:90]}", file=sys.stderr)
            time.sleep(4)                                   # Server nicht überlasten
            return elements
        return []

    @staticmethod
    def _post(url, ql):
        for attempt in range(2):
            r = requests.post(url, data={"data": ql}, timeout=120, headers=HEADERS)
            if r.status_code == 429 and attempt == 0:       # zu viele Anfragen: kurz warten, nochmal
                print(f"  ... {url.split('/')[2]}: Server bremst, warte 20 s", file=sys.stderr)
                time.sleep(20)
                continue
            r.raise_for_status()
            return r.json()

    def ways(self, names):
        ql = f'[out:json][timeout:60];way["waterway"]["name"~"^({"|".join(names)})$"]({BBOX});out geom;'
        return [[[p["lat"], p["lon"]] for p in el["geometry"]]
                for el in self.query(ql) if el.get("geometry")]

    def anchors(self, pattern):
        ql = f'[out:json][timeout:60];nwr["name"~"{pattern}"]({BBOX});out center;'
        out = []
        for el in self.query(ql):
            c = el if "lat" in el else el.get("center")
            if c and [c["lat"], c["lon"]] not in out:
                out.append([c["lat"], c["lon"]])
        return out[:3]


def clip(line, anchors, radius):
    runs, cur = [], []
    for lat, lon in line:
        if any(hav(lat, lon, a[0], a[1]) <= radius for a in anchors):
            cur.append([lat, lon])
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


def build_geometry(rule, geo):
    out = []
    for m in rule.get("manual", []):
        if "line" in m:
            out.append({"t": "line", "c": m["line"]})
        if "point" in m:
            out.append({"t": "circle", "c": m["point"], "r": m.get("radius_m", 200)})
    if out:
        return out
    radius = rule.get("radius_m", 300)
    anchors = geo.anchors(rule["near"]) if rule.get("near") else []
    if rule.get("ways") and rule.get("near") and not anchors:
        return []            # Abschnitt nicht bestimmbar -> lieber "nicht verortet" als die ganze Linie
    lines = geo.ways(rule["ways"]) if rule.get("ways") else []
    if lines and anchors:
        lines = [seg for l in lines for seg in clip(l, anchors, radius)]
    if lines:
        return [{"t": "line", "c": l} for l in lines]
    if anchors and not rule.get("ways"):
        return [{"t": "circle", "c": a, "r": radius} for a in anchors]
    return []


def find_rule(rules, row):
    g, b = row["gewaesser"].lower(), row["bereich"].lower()
    for rule in rules:
        if rule["match"].lower() in g and rule.get("bereich", "").lower() in b:
            return rule
    return None


# ----------------------------------------------------------------------------- 4. Karte
def render(items, day, stand, out):
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    data = {"datum": f"{WEEKDAYS[day.weekday()]}, {day.strftime('%d.%m.%Y')}",
            "iso": day.isoformat(), "erzeugt": berlin_now().strftime("%d.%m.%Y %H:%M"),
            "stand": stand, "items": items}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(tpl.replace("__DATA__", payload), encoding="utf-8")
    Path(out).with_name("sperrungen.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (Standard: heute)")
    ap.add_argument("--html", help="lokale HTML-Datei statt Live-Seite (Test)")
    ap.add_argument("--no-osm", action="store_true", help="keine Overpass-Abfragen (nur Cache/manual)")
    ap.add_argument("--lookahead", type=int, default=7, help="Tage, für die 'bald' angezeigt wird")
    ap.add_argument("--out", default=str(HERE / "docs" / "index.html"))
    a = ap.parse_args()
    day = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else berlin_now().date()

    rows, stand = parse_rows(fetch_html(a.html))
    if not rows:                                            # Seitenstruktur geändert? Lieber laut scheitern
        sys.exit("FEHLER: keine Tabellenzeilen gefunden - Seitenstruktur geändert? Karte NICHT aktualisiert.")

    rules = yaml.safe_load((HERE / "gewaesser.yaml").read_text(encoding="utf-8"))["regeln"]
    geo = Geo(a.no_osm)
    items = []
    for row in rows:
        st = status_on(row, day, a.lookahead)
        if st is None:
            continue
        row["status"] = st
        row["geom"], row["genau"] = [], True
        rule = find_rule(rules, row)
        if rule and st != "gebiet":
            row["geom"] = build_geometry(rule, geo)
            row["genau"] = rule.get("genau", True)
        row["paddel"] = bool(re.search(r"paddelboote können.*(umgetragen|ungetragen)", row["hinweis"], re.I))
        items.append(row)

    render(items, day, stand, a.out)
    for i in items:
        print(f"{i['status']:15} {'auf Karte ' if i['geom'] else 'OHNE Geo  '} {i['gewaesser']} | {i['bereich'][:60]}")
    print(f"\n{len(items)} Einträge für {day} -> {a.out}")


if __name__ == "__main__":
    main()
