# Main Line Trades Automation

This repository powers the Main Line Trades Discord automation system.

## Development setup

Python 3.10 or newer is required.

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate it with:

```powershell
.venv\Scripts\Activate.ps1
```

Install the project dependencies:

```bash
python -m pip install .
```

Run the complete non-posting test suite:

```bash
python -m unittest discover -v
```

## Current Automations

### 🎥 YouTube Replay Bot

Posts completed livestream replays automatically.

### 🎓 Training Video Feed

Checks the curated Main Line Trades training playlist every 30 minutes and
posts newly added tutorials as bordered Discord cards. Its processed-video
state is independent from the livestream replay feed.

### 🌅 Morning Brief

Posts every weekday before the New York Open with:

- High-impact USD events
- Major earnings
- Market snapshot
- Key markets
- Daily trading reminder
- Livestream reminder

### 🗓 Economic Calendar

Posts scheduled high-impact USD economic events.

---

## Repository Structure

```
.github/workflows/
scripts/
data/
```

---

Future automations:

- TrendSpider formatter
- Trump formatter
- Daily Watchlist
- Weekly Market Recap
- Market Dashboard
