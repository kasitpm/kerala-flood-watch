Kerala Flood Watch
A live rainfall and flood-risk dashboard for Kerala's 14 districts, built in Power BI and backed by a self-updating data pipeline. No manual refresh needed — the underlying data refreshes itself every 3 hours.
Author: B Kasinath (GitHub · LinkedIn · Portfolio)
---
What it does
The dashboard has two pages:
1. Rainfall Overview — 7-day past and forecast rainfall for all 14 Kerala districts, a colour-graded map and table (green → yellow → red), and live official weather/flood alerts.
2. Flood Watch — discharge levels for 5 major Kerala rivers (Periyar, Pamba, Bharathapuzha, Chalakudy, Karamana), each compared against its own historical high-water mark rather than a single fixed number.
That second part is the core idea of the project: a small river running unusually high for itself is just as significant as a large river doing the same, even though their absolute discharge numbers are nowhere close. A raw-number comparison would hide this entirely. In the current data, Karamana — despite having by far the smallest discharge of the five rivers — is proportionally the closest to its own 30-day threshold, a result a naive "biggest river = biggest risk" view would have missed.
Architecture
```
Open-Meteo (rainfall + river discharge)  ─┐
NDMA SACHET (official CAP alert feed)    ─┼─→  fetch_data.py  ─→  CSV files in /data  ─→  Power BI
                                           ┘         │
                                     GitHub Actions (runs every 3 hours)
```
`fetch_data.py` — a Python script that pulls rainfall and river-discharge forecasts from the Open-Meteo API, and parses official weather/flood alerts from the NDMA SACHET CAP feed (which aggregates warnings from IMD, the Central Water Commission, and state disaster authorities).
GitHub Actions runs this script on a schedule, with no server or paid hosting required, and commits the results back to this repo as CSV files.
Power BI Desktop (free tier) reads those CSVs directly from GitHub's raw file URLs, so the report picks up new data on every refresh without needing a Power BI Service subscription.
Data files
File	Contents
`data/rainfall_latest.csv`	14 districts × 14 days (7 past, 7 forecast) of precipitation
`data/rainfall_history.csv`	One daily snapshot per district, appended over time — used for forecast-vs-actual accuracy
`data/flood_latest.csv`	5 rivers × ~37 days of discharge (30 past, today, 7 forecast)
`data/alerts.csv`	Parsed official alerts, deduplicated, with a Red/Orange/Yellow severity mapping
`data/state.json`	ETags and processed-alert tracking, so the script avoids re-downloading unchanged data
The flood-watch calculation
Instead of a fixed discharge threshold, each river is measured against its own recent history:
```dax
Discharge P90 (Past 30d) =
CALCULATE(
    PERCENTILE.INC(Flood[Discharge], 0.9),
    Flood[Period] = "Past"
)

Flood Watch =
IF([Max Forecast Discharge] > [Discharge P90 (Past 30d)], "Watch", "Normal")
```
A river is flagged "Watch" when its forecast discharge exceeds the top 10% of its own last 30 days — a self-relative signal that works across rivers of very different sizes.
Data sources and limitations
Rainfall and river discharge are model-based (Open-Meteo's weather and GloFAS flood models), not direct rain-gauge or river-gauge readings. They're a reasonable proxy but shouldn't be treated as official measurements.
Alerts are sourced from NDMA's official SACHET feed, which itself aggregates IMD, CWC, and state disaster management alerts — but this dashboard is an independent, informational project, not an official warning system. For real emergencies, always follow guidance from KSDMA, IMD, and local authorities.
River coordinates were placed using publicly available geographic references. Open-Meteo's flood API operates at ~5 km resolution, so a coordinate that drifts off the exact river channel can return near-zero discharge; the fetch script logs a warning automatically if this happens for any river.
Tech stack
Python (requests, xml.etree) · GitHub Actions · Power BI Desktop, Power Query (M), DAX
Running it yourself
Fork this repo.
Enable GitHub Actions (Settings → Actions → General → set to "Read and write permissions").
Run the `Update Kerala data` workflow once manually, or wait for its scheduled run.
Open `Kerala_Flood_Watch.pbix` in Power BI Desktop — the queries already point at this repo's raw CSV URLs, so update them to point at your fork if you want independent data.
---
Built as a portfolio project to demonstrate live-data pipelines, DAX, and dashboard design for a Data Analyst role.
