# Network Monitor

Logs your internet connectivity and speed to a CSV and shows a live dashboard.
Built to give your ISP hard evidence of slow speeds and dropouts over a few days.

## What it does

- **Pings** Google DNS (`8.8.8.8`) and Cloudflare DNS (`1.1.1.1`) every 30 seconds → catches dropouts and latency spikes.
- **Speed test** via Ookla's official CLI every 30 minutes → catches slow throughput (~1–2 GB/day).
- **Manual button** to run a speed test the moment you notice slowness (logged separately as `speedtest_manual`).
- **Live dashboard** at `http://localhost:8000` with latency and speed charts, plus a dropout counter.
- Everything is appended to `network_log.csv` immediately, so a crash or reboot never loses past data — you'd only see a gap.

## One-time setup

1. **Get Ookla Speedtest CLI** (the official one, not the pip package):
   - Download the Windows build from https://www.speedtest.net/apps/cli
   - Unzip it and copy `speedtest.exe` into this folder (next to `monitor.py`).

2. **Install the Python packages** with [uv](https://docs.astral.sh/uv/) (in VSCode's terminal, in this folder):
   ```
   uv sync
   ```
   This creates `.venv` with the right Python and the exact versions in `uv.lock`.
   In VSCode, select the `.venv` interpreter (Ctrl+Shift+P → "Python: Select Interpreter").

## Running it

```
uv run python script/monitor.py
```

Then open **http://localhost:8000** in your browser. Leave the terminal running.
Stop with **Ctrl+C**.

## Important: stop your PC from sleeping

If the PC sleeps, logging stops and it will look like an outage that wasn't real —
you don't want to overstate the problem. Set Windows to never sleep while plugged in:

Settings → System → Power → Screen and sleep → set **"When plugged in, put my device to sleep after"** to **Never** for the monitoring days.

## Showing your ISP

- Open `network_log.csv` in Excel.
- The `type` column separates `ping`, `speedtest_auto`, and `speedtest_manual` rows.
- `success = False` rows are dropouts. `download_mbps` / `upload_mbps` show throughput.
- Chart `ping_ms` and `download_mbps` against `timestamp` — that's the picture that makes the case.

## Adjusting

At the top of `monitor.py`:

- `PING_INTERVAL_SEC` — how often to ping (default 30s).
- `SPEED_INTERVAL_SEC` — auto speed-test interval (default 30 min). Lower = more data used.
- `PING_TARGETS` — hosts to ping.
