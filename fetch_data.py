"""Kerala Flood Watch - data fetcher (runs on GitHub Actions).

Writes CSV files into ./data for Power BI to read:
  rainfall_latest.csv   14 districts x 14 days (7 past + 7 forecast), overwritten every run
  rainfall_history.csv  one snapshot of the above per day, appended (for forecast-vs-actual later)
  alerts.csv            official alerts from the NDMA SACHET Kerala CAP feed, appended
  state.json            ETags and already-seen alerts, so unchanged data is not downloaded again

Sources:
  Rainfall: Open-Meteo forecast API (model-based, not rain-gauge readings)
  Alerts:   https://sachet.ndma.gov.in/cap_public_website/rss/rss_kerala.xml
"""
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path("data")

RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_kerala.xml"
RAIN_URL = "https://api.open-meteo.com/v1/forecast"
CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
HEADERS = {"User-Agent": "kerala-flood-watch (student portfolio project; github.com/kasitpm)"}

# District headquarters (approximate coordinates)
DISTRICTS = [
    ("Thiruvananthapuram", 8.5241, 76.9366),
    ("Kollam", 8.8932, 76.6141),
    ("Pathanamthitta", 9.2648, 76.7870),
    ("Alappuzha", 9.4981, 76.3388),
    ("Kottayam", 9.5916, 76.5222),
    ("Idukki", 9.85, 76.94),
    ("Ernakulam", 9.9312, 76.2673),
    ("Thrissur", 10.5276, 76.2144),
    ("Palakkad", 10.7867, 76.6548),
    ("Malappuram", 11.0510, 76.0711),
    ("Kozhikode", 11.2588, 75.7804),
    ("Wayanad", 11.6103, 76.0830),
    ("Kannur", 11.8745, 75.3704),
    ("Kasaragod", 12.4996, 74.9869),
]

# Rivers for the flood-discharge layer. Coordinates are a starting point -
# check the printed discharge values after a run; a value stuck near 0 for
# days means the point has drifted off the river channel and needs nudging.
RIVERS = [
    ("Periyar", 10.1167, 76.3500),
    ("Pamba", 9.3167, 76.6167),
    ("Bharathapuzha", 10.8439, 76.0328),
    ("Chalakudy", 10.3000, 76.3333),
    ("Karamana", 8.5000, 77.0000),
]

FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
FLOOD_FIELDS = ["River", "Lat", "Lon", "Date", "Discharge", "Period", "Fetched_at"]

# Spellings that may appear in alert text
DISTRICT_ALIASES = {
    "Thiruvananthapuram": ["thiruvananthapuram", "trivandrum"],
    "Kollam": ["kollam", "quilon"],
    "Pathanamthitta": ["pathanamthitta"],
    "Alappuzha": ["alappuzha", "alleppey"],
    "Kottayam": ["kottayam"],
    "Idukki": ["idukki"],
    "Ernakulam": ["ernakulam"],
    "Thrissur": ["thrissur", "trichur"],
    "Palakkad": ["palakkad", "palghat"],
    "Malappuram": ["malappuram"],
    "Kozhikode": ["kozhikode", "calicut"],
    "Wayanad": ["wayanad"],
    "Kannur": ["kannur", "cannanore"],
    "Kasaragod": ["kasaragod", "kasargod", "kasaragode"],
}

LEVEL_RANK = {"Red": 3, "Orange": 2, "Yellow": 1, "Info": 0}

RAIN_FIELDS = ["District", "Lat", "Lon", "Date", "Rain_mm", "Rain_Prob", "Period", "Fetched_at"]
ALERT_FIELDS = [
    "Identifier", "District", "Event", "Level", "Level_Rank", "Level_Basis",
    "Severity", "Urgency", "Certainty", "Is_Flood", "Msg_Type",
    "Sent", "Effective", "Expires", "Headline", "Sender", "Source_URL",
]


# ---------------------------------------------------------------- helpers
def get_json(url, params, tries=5, timeout=60):
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  attempt {attempt} failed: {exc}")
            if attempt < tries:
                time.sleep(5 * attempt)
    return None


def fetch_conditional(url, etag=""):
    """GET with If-None-Match. Returns (content, etag); content is None on 304."""
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 304:
        return None, etag
    r.raise_for_status()
    return r.content, r.headers.get("ETag", "")


def to_ist(value):
    """ISO timestamp with offset -> 'YYYY-MM-DD HH:MM:SS' in IST (Power BI friendly)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def write_csv(path, fields, rows, append=False):
    new_file = not path.exists() or not append
    with path.open("a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def load_state():
    path = DATA_DIR / "state.json"
    state = {"rss_etag": "", "seen": {}, "cap_etags": {}}
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            print("state.json unreadable, starting fresh")
    return state


def save_state(state):
    (DATA_DIR / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------- rainfall
def rain_params(lat, lon):
    return {
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_sum,precipitation_probability_max",
        "timezone": "Asia/Kolkata",
        "past_days": 7,
        "forecast_days": 7,
    }


def fetch_rainfall(now):
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # One request for all 14 districts (Open-Meteo accepts comma-separated coordinates)
    results = get_json(RAIN_URL, rain_params(
        ",".join(str(d[1]) for d in DISTRICTS),
        ",".join(str(d[2]) for d in DISTRICTS),
    ))
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list) or len(results) != len(DISTRICTS):
        print("! batch request failed - falling back to one request per district")
        results = []
        for name, lat, lon in DISTRICTS:
            results.append(get_json(RAIN_URL, rain_params(lat, lon), tries=4))
            time.sleep(1)

    rows = []
    for (name, lat, lon), data in zip(DISTRICTS, results):
        if not data or "daily" not in data:
            print(f"! no rainfall data for {name}")
            continue
        d = data["daily"]
        probs = d.get("precipitation_probability_max") or [None] * len(d["time"])
        for day, mm, prob in zip(d["time"], d["precipitation_sum"], probs):
            rows.append({
                "District": name, "Lat": lat, "Lon": lon, "Date": day,
                "Rain_mm": "" if mm is None else mm,
                "Rain_Prob": "" if prob is None else prob,
                "Period": "Past" if day < today else "Forecast",
                "Fetched_at": stamp,
            })
    return rows


def update_rainfall(now):
    rows = fetch_rainfall(now)
    got = {r["District"] for r in rows}
    latest = DATA_DIR / "rainfall_latest.csv"
    if got != {d[0] for d in DISTRICTS}:
        if latest.exists() or not rows:
            print("! rainfall incomplete - keeping the previous file")
        else:
            print("! rainfall incomplete - writing partial data (no previous file yet)")
            write_csv(latest, RAIN_FIELDS, rows)
        return False
    write_csv(latest, RAIN_FIELDS, rows)

    # One snapshot per day for forecast-vs-actual analysis later
    today = now.date().isoformat()
    hist = DATA_DIR / "rainfall_history.csv"
    if hist.exists():
        with hist.open(newline="", encoding="utf-8") as f:
            if any(r["Fetched_date"] == today for r in csv.DictReader(f)):
                print(f"rainfall: {len(rows)} rows written (history already has today)")
                return True
    snap = [dict(r, Fetched_date=today) for r in rows]
    write_csv(hist, RAIN_FIELDS + ["Fetched_date"], snap, append=True)
    print(f"rainfall: {len(rows)} rows written + history snapshot")
    return True


# ----------------------------------------------------------------- flood
def fetch_flood(now):
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for name, lat, lon in RIVERS:
        data = get_json(FLOOD_URL, {
            "latitude": lat,
            "longitude": lon,
            "daily": "river_discharge",
            "timezone": "Asia/Kolkata",
            "past_days": 30,
            "forecast_days": 7,
        }, tries=4, timeout=45)
        if not data or "daily" not in data:
            print(f"! no flood data for {name}")
            continue
        d = data["daily"]
        values = d.get("river_discharge") or []
        non_null = [v for v in values if v is not None]
        if non_null and max(non_null) < 0.5:
            print(f"! {name}: discharge stuck near 0 (max {max(non_null):.2f}) - "
                  f"coordinates may be off the river channel, check on a map")
        for day, val in zip(d["time"], values):
            rows.append({
                "River": name, "Lat": lat, "Lon": lon, "Date": day,
                "Discharge": "" if val is None else val,
                "Period": "Past" if day < today else "Forecast",
                "Fetched_at": stamp,
            })
        time.sleep(0.5)
    return rows


def update_flood(now):
    rows = fetch_flood(now)
    got = {r["River"] for r in rows}
    latest = DATA_DIR / "flood_latest.csv"
    if got != {r[0] for r in RIVERS}:
        if latest.exists() or not rows:
            print("! flood data incomplete - keeping the previous file")
        else:
            print("! flood data incomplete - writing partial data (no previous file yet)")
            write_csv(latest, FLOOD_FIELDS, rows)
        return False
    write_csv(latest, FLOOD_FIELDS, rows)
    print(f"flood: {len(rows)} rows written")
    return True


# ----------------------------------------------------------------- alerts
def alert_level(event, severity):
    """Map an alert to Red/Orange/Yellow. Rain events follow IMD's wording;
    everything else falls back to the CAP severity. Level_Basis says which was used."""
    e = event.lower()
    if "extremely heavy" in e:
        return "Red", "event"
    if "very heavy" in e:
        return "Orange", "event"
    if "heavy" in e:
        return "Yellow", "event"
    by_severity = {"extreme": "Red", "severe": "Orange", "moderate": "Yellow"}
    return by_severity.get(severity.lower(), "Info"), "severity"


def match_districts(text):
    text = text.lower()
    found = []
    for name, aliases in DISTRICT_ALIASES.items():
        if any(re.search(rf"\b{a}\b", text) for a in aliases):
            found.append(name)
    return found


def _t(el, tag):
    node = el.find(f"cap:{tag}", CAP_NS)
    return (node.text or "").strip() if node is not None else ""


def parse_cap(xml_bytes, source_url, author=""):
    """One CAP message -> one row per affected district."""
    root = ET.fromstring(xml_bytes)
    if root.tag != f"{{{CAP_NS['cap']}}}alert":
        raise ValueError(f"unexpected root element: {root.tag}")
    infos = root.findall("cap:info", CAP_NS)
    if not infos:
        return []
    info = next((i for i in infos if _t(i, "language").lower().startswith("en")), infos[0])

    event = _t(info, "event")
    severity = _t(info, "severity")
    headline = _t(info, "headline")
    areas = [_t(a, "areaDesc") for a in info.findall("cap:area", CAP_NS)]
    districts = match_districts(" ".join([headline, _t(info, "description")] + areas)) or ["Unspecified"]
    level, basis = alert_level(event, severity)

    base = {
        "Identifier": _t(root, "identifier"),
        "Event": event,
        "Level": level,
        "Level_Rank": LEVEL_RANK[level],
        "Level_Basis": basis,
        "Severity": severity,
        "Urgency": _t(info, "urgency"),
        "Certainty": _t(info, "certainty"),
        "Is_Flood": "Yes" if "flood" in f"{event} {headline}".lower() else "No",
        "Msg_Type": _t(root, "msgType"),
        "Sent": to_ist(_t(root, "sent")),
        "Effective": to_ist(_t(info, "effective")),
        "Expires": to_ist(_t(info, "expires")),
        "Headline": headline,
        "Sender": _t(root, "sender") or author,
        "Source_URL": source_url,
    }
    return [dict(base, District=d) for d in districts]


def parse_rss(content):
    root = ET.fromstring(content)
    items = []
    for it in root.iter("item"):
        items.append({
            "guid": (it.findtext("guid") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "pubdate": (it.findtext("pubDate") or "").strip(),
            "author": (it.findtext("author") or "").strip(),
        })
    return [i for i in items if i["guid"] and i["link"]]


def update_alerts(state):
    path = DATA_DIR / "alerts.csv"
    if not path.exists():
        write_csv(path, ALERT_FIELDS, [])  # header only, so Power BI always finds the file
    with path.open(newline="", encoding="utf-8") as f:
        existing = {(r["Identifier"], r["District"]) for r in csv.DictReader(f)}

    rss, etag = fetch_conditional(RSS_URL, state.get("rss_etag", ""))
    if rss is None:
        print("alerts: feed unchanged (304)")
        return True

    items = parse_rss(rss)
    ok = True
    new_rows = []
    for it in items:
        guid = it["guid"]
        if state["seen"].get(guid) == it["pubdate"]:
            continue
        try:
            xml, cap_etag = fetch_conditional(it["link"], state["cap_etags"].get(guid, ""))
            if xml is not None:
                for row in parse_cap(xml, it["link"], it["author"]):
                    key = (row["Identifier"], row["District"])
                    if key not in existing:
                        existing.add(key)
                        new_rows.append(row)
            state["seen"][guid] = it["pubdate"]
            state["cap_etags"][guid] = cap_etag
        except Exception as exc:  # one bad alert must not stop the rest
            print(f"! could not process alert {guid}: {exc}")
            ok = False
        time.sleep(0.5)

    if new_rows:
        write_csv(path, ALERT_FIELDS, new_rows, append=True)
    print(f"alerts: {len(new_rows)} new rows from {len(items)} feed items")

    live = {i["guid"] for i in items}  # forget alerts that dropped out of the feed
    state["seen"] = {k: v for k, v in state["seen"].items() if k in live}
    state["cap_etags"] = {k: v for k, v in state["cap_etags"].items() if k in live}
    if ok:
        state["rss_etag"] = etag  # only remember it if every item was handled
    return ok


# ------------------------------------------------------------------- main
def main():
    DATA_DIR.mkdir(exist_ok=True)
    now = datetime.now(IST)
    state = load_state()
    ok = True
    try:
        ok &= update_rainfall(now)
    except Exception as exc:
        print(f"! rainfall step failed: {exc}")
        ok = False
    try:
        ok &= update_flood(now)
    except Exception as exc:
        print(f"! flood step failed: {exc}")
        ok = False
    try:
        ok &= update_alerts(state)
    except Exception as exc:
        print(f"! alerts step failed: {exc}")
        ok = False
    save_state(state)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
