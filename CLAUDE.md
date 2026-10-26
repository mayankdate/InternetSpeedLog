# InternetSpeedLog: contributor and AI coding guidance

## Purpose

InternetSpeedLog is a local network monitor that runs periodic internet speed
tests, appends results to a CSV log, and generates a self-contained HTML report
with charts.

## Architecture

```
InternetSpeedLog/
├── script/
│   ├── monitor.py          ← runs speed tests, appends to network_log.csv
│   ├── make_report.py      ← reads network_log.csv, renders HTML report
│   ├── speedtest.exe       ← Ookla CLI binary (Windows); keep out of git
│   └── chart.umd.min.js    ← Chart.js bundled for offline report rendering
├── pyproject.toml          ← Python packages (managed with uv)
├── uv.lock                 ← exact package versions; commit with pyproject.toml
├── .python-version
└── .gitignore
```

### Key files

- `script/monitor.py` — calls the Ookla speedtest CLI, parses the JSON output,
  and appends one row per test to `network_log.csv` in the repo root (or a
  path configured inside the script). Designed to be scheduled (cron / Task
  Scheduler) for continuous logging.
- `script/make_report.py` — reads `network_log.csv` and writes a standalone
  HTML file with download/upload/latency time-series charts via Chart.js.
- `script/speedtest.exe` — the Ookla Speedtest CLI for Windows. This binary
  is large and should remain in `.gitignore`; users must download it from
  Ookla separately.

## Commands

```bash
# Install Python dependencies (creates .venv from uv.lock)
uv sync

# Run a single speed test and append to log
uv run python script/monitor.py

# Generate the HTML report from the log
uv run python script/make_report.py
```

## Change guidelines

- `network_log.csv` is a runtime output file; keep it gitignored.
- `speedtest.exe` is a large third-party binary; keep it gitignored. Document
  the download URL in the README if users need it.
- Do not introduce a web framework or server. The report is a generated static
  HTML file opened locally in a browser.
- Chart.js (`chart.umd.min.js`) is vendored for offline use; do not replace it
  with a CDN link that requires internet access to view the report.
- Preserve the CSV column schema. If you add columns, add them at the end and
  update the report reader to handle rows that are missing the new columns.
