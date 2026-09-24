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
import argparse, base64, json, math, os, re, sys, time
from collections import Counter
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
    last_err = None
    for attempt in range(3):
        if attempt:
            wait = 20 * attempt                             # 20s, dann 40s Pause vor dem naechsten Versuch
            print(f"  ... LBV-Seite nicht erreichbar, warte {wait}s und versuche es erneut ({attempt+1}/3)", file=sys.stderr)
            time.sleep(wait)
        try:
            r = requests.get(URL, timeout=30, headers={"User-Agent": "Spreewald-Sperrkarte/1.0 (intern)"})
            r.raise_for_status()
            r.encoding = r.apparent_encoding if not r.encoding else r.encoding
            return r.text
        except Exception as e:
            last_err = e
    raise last_err


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


# ----------------------------------------------------------------------------- 5. Änderungs-Benachrichtigung
STATE = HERE / "state" / "letzter_stand.json"
KEYS = ("gewaesser", "bereich", "zeitraum", "grund", "hinweis")
LABELS = {"zeitraum": "Zeitraum", "grund": "Grund", "hinweis": "Hinweis"}


def norm(t):
    return re.sub(r"\s+", " ", t or "").strip()


def sig(row):
    return tuple(norm(row[k]) for k in KEYS)


def compare(old_rows, new_rows):
    """-> (neu, geändert [(alt, neu)], entfernt); alles als Tupel in der Reihenfolge KEYS."""
    co, cn = Counter(sig(r) for r in old_rows), Counter(sig(r) for r in new_rows)
    added, removed, changed = list((cn - co).elements()), list((co - cn).elements()), []
    for a in added[:]:
        for r in removed:
            if r[:2] == a[:2]:                                # gleiches Gewässer + gleicher Bereich
                changed.append((r, a)); added.remove(a); removed.remove(r)
                break
    return added, changed, removed


def position_hint(t, rules):
    row = dict(zip(KEYS, t))
    if row["gewaesser"].lower().startswith("oberspreewald"):
        return "Gebietshinweis (keine Kartenposition nötig)"
    rule = find_rule(rules, row)
    if rule and (rule.get("manual") or rule.get("ways") or rule.get("near")):
        return "Position hinterlegt"
    return "ACHTUNG: KEINE Position hinterlegt -> bitte in gewaesser.yaml ergänzen"


def build_message(added, changed, removed, stand, rules):
    n = len(added) + len(changed) + len(removed)
    parts = []
    if added:
        parts.append(f"{len(added)} neu")
    if changed:
        parts.append(f"{len(changed)} geändert")
    if removed:
        parts.append(f"{len(removed)} entfernt")
    subject = "Spreewald-Sperrungen: " + ", ".join(parts)
    L = [f"Auf der LBV-Seite haben sich die Sperrungen geändert (LBV-Stand: {stand or 'unbekannt'}).", ""]
    if added:
        L += [f"NEU ({len(added)})", ""]
        for t in added:
            r = dict(zip(KEYS, t))
            L += [f"* {r['gewaesser']}", f"  Bereich:  {r['bereich']}", f"  Zeitraum: {r['zeitraum']}",
                  f"  Grund:    {r['grund']}", f"  Hinweis:  {r['hinweis']}",
                  f"  Karte:    {position_hint(t, rules)}", ""]
    if changed:
        L += [f"GEÄNDERT ({len(changed)})", ""]
        for old, new in changed:
            r = dict(zip(KEYS, new))
            L += [f"* {r['gewaesser']}", f"  Bereich:  {r['bereich']}"]
            for i, k in enumerate(KEYS):
                if k in LABELS and old[i] != new[i]:
                    L += [f"  {LABELS[k]} vorher:  {old[i]}", f"  {LABELS[k]} jetzt:   {new[i]}"]
            L.append("")
    if removed:
        L += [f"ENTFERNT / nicht mehr aufgeführt ({len(removed)})", ""]
        for t in removed:
            r = dict(zip(KEYS, t))
            L += [f"* {r['gewaesser']}", f"  Bereich:  {r['bereich']}", f"  Zeitraum: {r['zeitraum']}", ""]
    map_url = os.environ.get("MAP_URL")
    if map_url:
        L.append(f"Karte:      {map_url}")
    L += [f"LBV-Seite:  {URL}", "", "Diese Nachricht wurde automatisch erzeugt. Maßgeblich sind die LBV-Seite und die Beschilderung vor Ort."]
    return subject, "\n".join(L), n


def encode_header(text):
    """RFC-2047-Kodierung, damit Umlaute in HTTP-Kopfzeilen sicher ankommen."""
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send_ntfy(subject, body):
    """Push-Benachrichtigung über ntfy.sh (kostenlos, ohne Anmeldung). Doku: https://ntfy.sh/docs/"""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (Push nicht konfiguriert: NTFY_TOPIC fehlt)", file=sys.stderr)
        return False
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                          headers={"Title": encode_header(subject), "Priority": "default"}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ! Push-Versand fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def save_state(rows, stand, state_file):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"stand": stand, "rows": [dict(zip(KEYS, sig(r))) for r in rows]},
                                     ensure_ascii=False, indent=1), encoding="utf-8")


def notify_changes(rows, stand, rules, state_file):
    if not state_file.exists():
        save_state(rows, stand, state_file)
        print("Änderungs-Benachrichtigung: erster Lauf, Ausgangsstand gespeichert (keine Nachricht).")
        return
    old = json.loads(state_file.read_text(encoding="utf-8"))["rows"]
    added, changed, removed = compare(old, rows)
    if not (added or changed or removed):
        print("Änderungs-Benachrichtigung: keine Änderungen seit dem letzten Lauf.")
        return
    subject, body, n = build_message(added, changed, removed, stand, rules)
    print(f"Änderungs-Benachrichtigung: {n} Änderung(en) erkannt -> {subject}")
    delivered = False
    issue_file = os.environ.get("ISSUE_FILE")            # GitHub-Hinweis: Workflow legt daraus ein Issue an
    if issue_file:
        users = " ".join(u if u.startswith("@") else "@" + u
                         for u in re.split(r"[\s,;]+", os.environ.get("NOTIFY_USERS", "")) if u)
        text = (users + "\n\n" if users else "") + "```text\n" + body + "\n```\n"
        Path(issue_file).write_text(text, encoding="utf-8")
        Path(issue_file).with_suffix(".title").write_text(subject, encoding="utf-8")
        print(f"  GitHub-Hinweis vorbereitet ({issue_file}).")
        delivered = True
    if os.environ.get("NTFY_TOPIC"):
        if send_ntfy(subject, body):
            print("  Push-Nachricht gesendet.")
            delivered = True
    if delivered:
        save_state(rows, stand, state_file)               # nur nach erfolgreicher Zustellung merken
    else:
        print("::warning::Änderungen erkannt, aber kein Kanal (Push/GitHub-Hinweis) aktiv - beim nächsten Lauf erneut versucht.")


def load_own_notices(path, day, lookahead):
    """Liest eigene, von Mitarbeitern gepflegte Hinweise (eigene-sperrungen.yaml).
    Fehlerhafte einzelne Einträge werden übersprungen (mit Warnung), statt den ganzen Lauf abzubrechen."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"::warning::eigene-sperrungen.yaml konnte nicht gelesen werden: {e}", file=sys.stderr)
        return []
    items = []
    for i, e in enumerate(data.get("hinweise") or [], start=1):
        try:
            gew = str(e["gewaesser"]).strip()
            bereich = str(e.get("bereich", "")).strip()
            grund = str(e.get("grund", "")).strip()
            hinweis = str(e.get("hinweis", "")).strip()
            status = str(e.get("status", "gesperrt")).strip().lower()
            if status not in ("gesperrt", "eingeschraenkt"):
                raise ValueError(f"status muss 'gesperrt' oder 'eingeschraenkt' sein, nicht '{status}'")
            von_txt, bis_txt = str(e.get("von", "")).strip(), str(e.get("bis", "")).strip()
            von = datetime.strptime(von_txt, "%d.%m.%Y").date() if von_txt else None
            bis = datetime.strptime(bis_txt, "%d.%m.%Y").date() if bis_txt else None
            if von and day < von:
                if (von - day).days > lookahead:
                    continue
                st = "bald"
            elif bis and day > bis:
                continue
            else:
                st = status
            zeitraum = (f"ab {von.strftime('%d.%m.%Y')}" if von and not bis else
                        f"{von.strftime('%d.%m.%Y')} bis {bis.strftime('%d.%m.%Y')}" if von and bis else
                        f"bis {bis.strftime('%d.%m.%Y')}" if bis else "bis auf Weiteres")
            geom = []
            if "punkt" in e:
                geom = [{"t": "circle", "c": [float(e["punkt"][0]), float(e["punkt"][1])],
                        "r": int(e.get("radius_m", 200))}]
            elif "linie" in e:
                geom = [{"t": "line", "c": [[float(p[0]), float(p[1])] for p in e["linie"]]}]
            items.append(dict(gewaesser=gew, bereich=bereich, zeitraum=zeitraum, grund=grund, hinweis=hinweis,
                              status=st, start=von.isoformat() if von else None, ende=bis.isoformat() if bis else None,
                              geom=geom, genau=True, paddel=False, quelle="eigen"))
        except Exception as ex:
            print(f"::warning::eigene-sperrungen.yaml, Eintrag {i} übersprungen ({ex})", file=sys.stderr)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (Standard: heute)")
    ap.add_argument("--html", help="lokale HTML-Datei statt Live-Seite (Test)")
    ap.add_argument("--no-osm", action="store_true", help="keine Overpass-Abfragen (nur Cache/manual)")
    ap.add_argument("--lookahead", type=int, default=7, help="Tage, für die 'bald' angezeigt wird")
    ap.add_argument("--notify", action="store_true", help="bei Änderungen der LBV-Tabelle eine Push-Nachricht/GitHub-Hinweis senden")
    ap.add_argument("--test-push", action="store_true", help="nur eine Test-Push-Nachricht senden (ntfy-Konfiguration prüfen)")
    ap.add_argument("--state-file", default=str(STATE))
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
        row["quelle"] = "lbv"
        items.append(row)

    eigene = load_own_notices(HERE / "eigene-sperrungen.yaml", day, a.lookahead)
    items.extend(eigene)

    render(items, day, stand, a.out)
    for i in items:
        marker = " (eigen)" if i.get("quelle") == "eigen" else ""
        print(f"{i['status']:15} {'auf Karte ' if i['geom'] else 'OHNE Geo  '} {i['gewaesser']}{marker} | {i['bereich'][:60]}")
    print(f"\n{len(items)} Einträge für {day} -> {a.out}")

    if a.notify:
        notify_changes(rows, stand, rules, Path(a.state_file))
    if a.test_push:
        subj = "Spreewald-Sperrkarte: Testnachricht"
        body = "Das ist eine Testnachricht der Spreewald-Sperrkarte. Wenn du sie liest, funktioniert der Push-Versand."
        if not os.environ.get("NTFY_TOPIC"):
            sys.exit("FEHLER: NTFY_TOPIC ist nicht konfiguriert.")
        if send_ntfy(subj, body):
            print("Test-Push gesendet.")
        else:
            sys.exit("FEHLER: Test-Push konnte nicht gesendet werden (Details siehe oben).")


if __name__ == "__main__":
    main()
