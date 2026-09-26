"""
Network monitor: logs connectivity (ping) and speed (Ookla CLI) to a CSV,
and serves a live dashboard with a manual "run speed test now" button.

Run:  python monitor.py
Open: http://localhost:8000
Stop: Ctrl+C in the terminal
"""

import csv
import os
import platform
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PING_TARGETS = ["8.8.8.8", "1.1.1.1"]   # Google DNS, Cloudflare DNS (raw IPs, no DNS needed)
DNS_TARGETS = ["discord.com", "youtube.com", "google.com", "claude.ai", "netflix.com"]  # resolve names to catch DNS failures
PING_INTERVAL_SEC = 90                   # how often to ping/DNS-check (1.5 min)
SPEED_INTERVAL_SEC = 30 * 60             # how often to auto speed test (30 min)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)  # one level up from script/
CSV_PATH = os.path.join(_PROJECT_DIR, "output", "network_log.csv")
SPEEDTEST_EXE = os.path.join(_SCRIPT_DIR, "speedtest.exe")
PORT = 8000

CSV_HEADER = [
    "timestamp", "type", "target",
    "ping_ms", "packet_loss_pct",
    "download_mbps", "upload_mbps",
    "success", "ssid",
]

# One lock so the ping thread, speed thread, and manual trigger never
# interleave writes to the same CSV line.
_csv_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def ensure_csv():
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def write_row(row: dict):
    """Append one row and flush immediately so a crash never loses history."""
    with _csv_lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([row.get(k, "") for k in CSV_HEADER])
            f.flush()
            os.fsync(f.fileno())


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# Cache the SSID briefly so we don't shell out on every single row.
_ssid_cache = {"value": "", "at": 0.0}


def current_ssid():
    """Return the connected WiFi network name, or '' if unknown / not on WiFi.

    Uses netsh on Windows. The result is cached for a few seconds because the
    ping and DNS rows in one cycle all share the same network.
    """
    now = time.monotonic()
    if now - _ssid_cache["at"] < 5:
        return _ssid_cache["value"]

    ssid = ""
    if platform.system().lower() == "windows":
        try:
            out = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            # Match the SSID line but NOT the BSSID line (hardware address).
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("ssid") and not stripped.lower().startswith("bssid"):
                    parts = stripped.split(":", 1)
                    if len(parts) == 2:
                        ssid = parts[1].strip()
                    break
        except (subprocess.TimeoutExpired, OSError):
            ssid = ""

    _ssid_cache["value"] = ssid
    _ssid_cache["at"] = now
    return ssid


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------
def ping_once(target: str):
    """Return (ping_ms, success). ping_ms is None on failure."""
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    # -w timeout (ms on Windows, but we keep it simple and cross-platform-ish)
    cmd = ["ping", count_flag, "1", target]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return None, False
    match = re.search(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms", result.stdout)
    ping_ms = float(match.group(1)) if match else None
    return ping_ms, True


def dns_once(hostname: str):
    """Try to resolve a domain name to an IP. Returns (resolve_ms, success).

    This is separate from ping: ping hits raw IPs and works even when DNS is
    broken. If pings to 8.8.8.8 succeed but these resolutions fail, the
    connection is up but name resolution is failing — the classic
    'some sites load, some don't' cause.
    """
    import socket
    start = time.monotonic()
    try:
        socket.gethostbyname(hostname)
        resolve_ms = (time.monotonic() - start) * 1000
        return resolve_ms, True
    except socket.gaierror:
        return None, False


def ping_loop():
    while True:
        ssid = current_ssid()
        for target in PING_TARGETS:
            ping_ms, success = ping_once(target)
            write_row({
                "timestamp": now_iso(),
                "type": "ping",
                "target": target,
                "ping_ms": ping_ms if ping_ms is not None else "",
                "packet_loss_pct": 0 if success else 100,
                "success": success,
                "ssid": ssid,
            })
        for host in DNS_TARGETS:
            resolve_ms, success = dns_once(host)
            write_row({
                "timestamp": now_iso(),
                "type": "dns",
                "target": host,
                "ping_ms": round(resolve_ms, 1) if resolve_ms is not None else "",
                "packet_loss_pct": 0 if success else 100,
                "success": success,
                "ssid": ssid,
            })
        time.sleep(PING_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Speed test (Ookla official CLI)
# ---------------------------------------------------------------------------
def run_speedtest(kind: str):
    """Run a speed test. Returns a result dict.

    A missing speedtest.exe is a setup error and is raised so it surfaces
    loudly. A test that runs but fails (e.g. connection down mid-test) is
    real evidence and gets logged as a failed speedtest row.
    """
    if not os.path.exists(SPEEDTEST_EXE):
        raise FileNotFoundError(
            f"speedtest.exe not found at {SPEEDTEST_EXE}. "
            "Download the Ookla CLI from https://www.speedtest.net/apps/cli "
            "and put speedtest.exe in the script folder."
        )
    cmd = [SPEEDTEST_EXE, "--format=json", "--accept-license", "--accept-gdpr"]
    import json
    ssid = current_ssid()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        data = json.loads(result.stdout)
        # Ookla reports bandwidth in bytes/sec; convert to megabits/sec.
        down = data["download"]["bandwidth"] * 8 / 1_000_000
        up = data["upload"]["bandwidth"] * 8 / 1_000_000
        latency = data["ping"]["latency"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        # The test ran but couldn't complete — that's a real network problem,
        # so record it as evidence rather than swallowing it.
        write_row({
            "timestamp": now_iso(),
            "type": kind,
            "target": "",
            "success": False,
            "ssid": ssid,
        })
        return {"error": str(e), "success": False}
    write_row({
        "timestamp": now_iso(),
        "type": kind,
        "target": data.get("server", {}).get("name", ""),
        "ping_ms": round(latency, 1),
        "download_mbps": round(down, 2),
        "upload_mbps": round(up, 2),
        "success": True,
        "ssid": ssid,
    })
    return {"download_mbps": round(down, 2), "upload_mbps": round(up, 2),
            "ping_ms": round(latency, 1), "success": True}


def speed_loop():
    while True:
        if os.path.exists(SPEEDTEST_EXE):
            run_speedtest("speedtest_auto")
        time.sleep(SPEED_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Monitor</title>
<script src="/chart.js"></script>
<style>
  :root {
    --ink: #0f1b2d; --paper: #f5f3ec; --line: #d9d4c6;
    --good: #2f7d4f; --bad: #c0392b; --accent: #1f6feb; --muted: #6b6355;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
  }
  header {
    padding: 20px 24px; border-bottom: 2px solid var(--ink);
    display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 12px;
  }
  h1 { font-size: 20px; margin: 0; letter-spacing: -0.5px; }
  .sub { color: var(--muted); font-size: 12px; }
  main { padding: 24px; max-width: 1000px; margin: 0 auto; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .stat { border: 1px solid var(--line); background: #fff; padding: 14px 16px; }
  .stat .label { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 1px; }
  .stat .value { font-size: 26px; font-weight: 700; margin-top: 4px; }
  .stat .value.bad { color: var(--bad); }
  .stat .value.good { color: var(--good); }
  .chart-box { border: 1px solid var(--line); background: #fff; padding: 16px; margin-bottom: 20px; }
  .chart-box h2 { font-size: 13px; margin: 0 0 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
  button {
    font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer;
    background: var(--ink); color: var(--paper); border: none; padding: 10px 18px;
  }
  button:disabled { opacity: 0.5; cursor: wait; }
  #status { font-size: 12px; color: var(--muted); margin-left: 12px; }
</style>
</head>
<body>
<div id="debug" style="font-family:monospace;font-size:12px;padding:8px 24px;background:#fff3cd;border-bottom:1px solid #d9d4c6;">
  diagnostics: <span id="dbg">starting…</span>
</div>
<header>
  <div>
    <h1>Network Monitor</h1>
    <div class="sub">Logging to network_log.csv &middot; auto speed test every 30 min</div>
  </div>
  <div>
    <button id="run" onclick="manualTest()">Run speed test now</button>
    <span id="status"></span>
  </div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div class="label">Current network</div><div class="value" id="s-net" style="font-size:18px;">&mdash;</div></div>
    <div class="stat"><div class="label">Last download</div><div class="value" id="s-down">&mdash;</div></div>
    <div class="stat"><div class="label">Last upload</div><div class="value" id="s-up">&mdash;</div></div>
    <div class="stat"><div class="label">Last ping</div><div class="value" id="s-ping">&mdash;</div></div>
    <div class="stat"><div class="label">Ping dropouts</div><div class="value" id="s-drops">&mdash;</div></div>
    <div class="stat"><div class="label">DNS failures</div><div class="value" id="s-dns">&mdash;</div></div>
  </div>
  <div class="chart-box">
    <h2>Reliability by network</h2>
    <table id="net-table" style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="text-align:left;color:var(--muted);">
        <th style="padding:6px 8px;">Network</th>
        <th style="padding:6px 8px;">Ping dropouts</th>
        <th style="padding:6px 8px;">DNS failures</th>
      </tr></thead>
      <tbody id="net-body"></tbody>
    </table>
  </div>
  <div class="chart-box"><h2>Latency (ms) over time</h2><canvas id="pingChart" height="90"></canvas></div>
  <div class="chart-box"><h2>DNS resolution (1 = worked, 0 = failed)</h2><canvas id="dnsChart" height="70"></canvas></div>
  <div class="chart-box"><h2>Speed (Mbps) over time</h2><canvas id="speedChart" height="90"></canvas></div>
</main>
<script>
let pingChart, dnsChart, speedChart;
// Turn an ISO timestamp into a short HH:MM label for the x-axis.
function label(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function mkChart(ctx, labels, datasets, yMax) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      animation: false, spanGaps: false,
      scales: {
        x: { ticks: { maxTicksLimit: 12, autoSkip: true, font: { family: 'monospace' } } },
        y: yMax != null ? { min: 0, max: yMax, ticks: { stepSize: 1 } } : { beginAtZero: true }
      },
      elements: { point: { radius: 0 } },
      plugins: { legend: { labels: { font: { family: 'monospace' } } } }
    }
  });
}
function setDbg(msg) { document.getElementById('dbg').textContent = msg; }

async function refresh() {
  try {
    if (typeof Chart === 'undefined') {
      setDbg('ERROR: Chart.js did not load. Is chart.umd.min.js in the script folder? Try opening http://localhost:8000/chart.js — it should show code, not an error.');
      return;
    }
    const r = await fetch('/data');
    if (!r.ok) { setDbg('ERROR: /data returned ' + r.status); return; }
    const d = await r.json();

    document.getElementById('s-net').textContent = d.current_network || '(unknown)';
    document.getElementById('s-down').textContent = d.last_download != null ? d.last_download + ' Mbps' : '—';
    document.getElementById('s-up').textContent   = d.last_upload   != null ? d.last_upload + ' Mbps' : '—';
    document.getElementById('s-ping').textContent = d.last_ping     != null ? d.last_ping + ' ms' : '—';
    const drops = document.getElementById('s-drops');
    drops.textContent = d.ping_dropouts;
    drops.className = 'value ' + (d.ping_dropouts > 0 ? 'bad' : 'good');
    const dns = document.getElementById('s-dns');
    dns.textContent = d.dns_failures;
    dns.className = 'value ' + (d.dns_failures > 0 ? 'bad' : 'good');

    const body = document.getElementById('net-body');
    body.innerHTML = '';
    for (const [name, s] of Object.entries(d.by_network || {})) {
      const tr = document.createElement('tr');
      tr.style.borderTop = '1px solid var(--line)';
      const dropColor = s.ping_drops > 0 ? 'var(--bad)' : 'var(--good)';
      const dnsColor = s.dns_fail > 0 ? 'var(--bad)' : 'var(--good)';
      tr.innerHTML =
        `<td style="padding:6px 8px;">${name}</td>` +
        `<td style="padding:6px 8px;color:${dropColor};">${s.ping_drops} / ${s.pings}</td>` +
        `<td style="padding:6px 8px;color:${dnsColor};">${s.dns_fail} / ${s.dns}</td>`;
      body.appendChild(tr);
    }

    const pingLabels = d.ping.map(p => label(p.t));
    const pingVals   = d.ping.map(p => p.v);
    const dnsLabels  = d.dns.map(p => label(p.t));
    const dnsVals    = d.dns.map(p => p.v);
    const speedLabels = d.download.map(p => label(p.t));
    const downVals   = d.download.map(p => p.v);
    const upVals     = d.upload.map(p => p.v);

    setDbg(`Chart.js OK · ping points: ${pingVals.length} · dns points: ${dnsVals.length} · speed points: ${downVals.length}` +
           (pingVals.length === 0 ? ' · (no data yet — wait for the first ping cycle)' : ''));

    if (!pingChart) {
      pingChart = mkChart(document.getElementById('pingChart'), pingLabels,
        [{ label: 'ping ms', data: pingVals, borderColor: '#1f6feb', borderWidth: 1.5 }]);
      dnsChart = mkChart(document.getElementById('dnsChart'), dnsLabels,
        [{ label: 'DNS ok', data: dnsVals, borderColor: '#c0392b', borderWidth: 1.5, stepped: true }], 1.1);
      speedChart = mkChart(document.getElementById('speedChart'), speedLabels,
        [{ label: 'download', data: downVals, borderColor: '#2f7d4f', borderWidth: 1.5 },
         { label: 'upload',   data: upVals,   borderColor: '#c0392b', borderWidth: 1.5 }]);
    } else {
      pingChart.data.labels = pingLabels;
      pingChart.data.datasets[0].data = pingVals;
      dnsChart.data.labels = dnsLabels;
      dnsChart.data.datasets[0].data = dnsVals;
      speedChart.data.labels = speedLabels;
      speedChart.data.datasets[0].data = downVals;
      speedChart.data.datasets[1].data = upVals;
      pingChart.update(); dnsChart.update(); speedChart.update();
    }
  } catch (e) {
    setDbg('ERROR in refresh(): ' + e.message);
  }
}
async function manualTest() {
  const btn = document.getElementById('run');
  const st = document.getElementById('status');
  btn.disabled = true; st.textContent = 'Running… (up to a minute)';
  try {
    const r = await fetch('/speedtest', { method: 'POST' });
    const d = await r.json();
    if (d.success) {
      st.textContent = `Done: ${d.download_mbps} down / ${d.upload_mbps} up`;
    } else {
      st.textContent = 'Speed test failed — logged as an outage. ' + (d.error || '');
    }
    refresh();
  } catch (e) {
    st.textContent = 'Speed test failed — check the terminal.';
  } finally { btn.disabled = false; }
}
refresh();
setInterval(refresh, 15000);  // live update every 15s
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/chart.js")
def chart_js():
    """Serve the bundled Chart.js so the dashboard needs no internet —
    essential since this tool runs precisely when the network is down."""
    path = os.path.join(_SCRIPT_DIR, "chart.umd.min.js")
    with open(path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "application/javascript"}


@app.route("/data")
def data():
    pings, dns_ok, downloads, uploads = [], [], [], []
    last_download = last_upload = last_ping = None
    ping_dropouts = 0
    dns_failures = 0
    # Per-network tallies: {ssid: {"pings": n, "ping_drops": n, "dns": n, "dns_fail": n}}
    by_network = {}
    with _csv_lock:
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    t = row["timestamp"]
                    rtype = row["type"]
                    failed = row["success"] == "False"
                    ssid = row.get("ssid") or "(unknown)"
                    net = by_network.setdefault(
                        ssid, {"pings": 0, "ping_drops": 0, "dns": 0, "dns_fail": 0})
                    if rtype == "ping":
                        net["pings"] += 1
                        if failed:
                            ping_dropouts += 1
                            net["ping_drops"] += 1
                        if row["ping_ms"]:
                            pings.append({"t": t, "v": float(row["ping_ms"])})
                            last_ping = float(row["ping_ms"])
                    elif rtype == "dns":
                        net["dns"] += 1
                        if failed:
                            dns_failures += 1
                            net["dns_fail"] += 1
                        # 1 = resolved, 0 = failed, so the chart shows a clear drop
                        dns_ok.append({"t": t, "v": 0 if failed else 1})
                    elif rtype.startswith("speedtest"):
                        if row["download_mbps"]:
                            downloads.append({"t": t, "v": float(row["download_mbps"])})
                            last_download = float(row["download_mbps"])
                        if row["upload_mbps"]:
                            uploads.append({"t": t, "v": float(row["upload_mbps"])})
                            last_upload = float(row["upload_mbps"])
    return jsonify({
        "ping": pings, "dns": dns_ok,
        "download": downloads, "upload": uploads,
        "last_download": last_download, "last_upload": last_upload,
        "last_ping": last_ping,
        "ping_dropouts": ping_dropouts, "dns_failures": dns_failures,
        "current_network": current_ssid() or "(unknown)",
        "by_network": by_network,
    })


@app.route("/speedtest", methods=["POST"])
def manual_speedtest():
    try:
        return jsonify(run_speedtest("speedtest_manual"))
    except FileNotFoundError as e:
        return jsonify({"error": str(e), "success": False}), 400


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ensure_csv()
    if not os.path.exists(os.path.join(_SCRIPT_DIR, "chart.umd.min.js")):
        print("=" * 60)
        print("WARNING: chart.umd.min.js not found in the script folder.")
        print("The dashboard charts will be blank without it.")
        print("Keep chart.umd.min.js next to monitor.py.")
        print("=" * 60)
    if not os.path.exists(SPEEDTEST_EXE):
        print("=" * 60)
        print("WARNING: speedtest.exe not found in the script folder.")
        print("Ping logging will work, but speed tests will not.")
        print("Download it from https://www.speedtest.net/apps/cli")
        print("and place speedtest.exe next to monitor.py.")
        print("=" * 60)
    threading.Thread(target=ping_loop, daemon=True).start()
    threading.Thread(target=speed_loop, daemon=True).start()
    print(f"Monitor running. Open http://localhost:{PORT}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    main()