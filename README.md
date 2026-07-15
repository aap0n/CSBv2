# Carleton Schedule Builder

An unofficial, fast, visual schedule builder for Carleton University course registration.

**Use it here → https://aap0n.github.io/carleton-schedule-builder/**

## Features

Search any course by code, title, or instructor — for **every term Carleton has opened** (pick the term in the header). Hover a section to preview it on the weekly calendar before committing; click to add it. Conflicts are flagged instantly, linked labs/tutorials ("also register in") are surfaced, and the right panel tracks your credits, class hours per week, and full semester build. Each term keeps its own saved schedule automatically in your browser. Export your CRNs for quick registration in Carleton Central, or download an .ics file to drop the whole schedule into your calendar app.

## Data

Course data is scraped automatically from Carleton Central's public course search (draft timetable) by [`scraper/scrape.py`](scraper/scrape.py), which runs on GitHub Actions every 6 hours and commits per-term files to [`data/`](data/) (`courses-<termCode>.json` plus a `terms.json` index). New terms are picked up automatically as Carleton opens them. The header shows the scrape date for the data you're looking at.

**Manual refresh:** Actions → "Scrape course data" → Run workflow.

Times and offerings may change before registration — always confirm in Carleton Central before registering.

## Development

The app is a single `index.html` (vanilla JS/CSS, no build step) that fetches its data from `data/`. Because it uses `fetch`, opening the file directly from disk won't work — serve it locally:

```sh
python3 -m http.server
# then open http://localhost:8000
```

Run the scraper locally with `python3 scraper/scrape.py` (Python 3 stdlib only, no dependencies; ~2–4 min per term).

## Disclaimer

Not affiliated with or endorsed by Carleton University. This is a planning aid; Carleton Central is the source of truth for registration.
