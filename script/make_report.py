"""
Build a plain-English internet quality report (PDF) from one or more
network_log.csv files written by monitor.py.

Usage:
    python make_report.py                                # ../output/network_log.csv
    python make_report.py a.csv b.csv                    # merge several logs
    python make_report.py -o report.pdf --compare "Home_5G,Home_2G"
    python make_report.py --plan-mbps 100                # judge speeds against a plan

Every number, date, network name, test interval and target in the report is
computed from the log. The only fixed values are the verdict thresholds in
THRESHOLDS, and the report prints them so readers can see how a verdict was
reached.
"""

import argparse
import csv
import io
import os
import sys
from datetime import timedelta, timezone
from xml.sax.saxutils import escape

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COLS = ["timestamp", "type", "target", "ping_ms", "packet_loss_pct",
        "download_mbps", "upload_mbps", "success", "ssid"]

NO_SSID = "(no network name)"   # blank ssid: logged before SSID tracking, or no WiFi

# Verdict rules. A network needs MIN_* samples before it is judged at all.
THRESHOLDS = {
    "min_pings": 50, "min_speed_tests": 3,
    "unusable_speed_fail_pct": 20, "unusable_ping_loss_pct": 5,
    "poor_speed_fail_pct": 5, "poor_ping_loss_pct": 1,
}
MIN_HOUR_SAMPLES = 20      # pings needed in an hour bucket before it is ranked
LOW_PLAN_FRACTION = 0.5    # with --plan-mbps: "slow" = below this share of the plan

INK = colors.HexColor("#0f1b2d")
BAD = colors.HexColor("#c0392b")
WARN = colors.HexColor("#c77d0a")
GOOD = colors.HexColor("#2f7d4f")
MUT = colors.HexColor("#6b6355")
LINE = colors.HexColor("#d9d4c6")
LIGHT = colors.HexColor("#f5f3ec")
TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), INK),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
    ("GRID", (0, 0), (-1, -1), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
])
PALETTE = ["#1f6feb", "#b5651d", "#2f7d4f", "#8e44ad", "#16a085", "#d35400"]


# ---------------------------------------------------------------------------
# Load & clean
# ---------------------------------------------------------------------------
def _local_tz(raw_ts):
    """Fixed-offset timezone matching the most common UTC offset in the log."""
    offsets = raw_ts.str.extract(r"([+-]\d{2}):?(\d{2})$").dropna()
    if offsets.empty:
        return timezone.utc
    h, m = offsets.value_counts().idxmax()
    sign = -1 if h.startswith("-") else 1
    return timezone(sign * timedelta(hours=abs(int(h)), minutes=int(m)))


def load_logs(paths):
    """Read and merge CSVs, tolerating the 8-column (no ssid) legacy schema."""
    rows = []
    for path in paths:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.reader(f):
                if len(r) >= 8 and r[0] != "timestamp":
                    rows.append((r + [""] * 9)[:9])
    df = pd.DataFrame(rows, columns=COLS)

    tz = _local_tz(df["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce",
                                     format="ISO8601").dt.tz_convert(tz)
    df = df.dropna(subset=["timestamp"])
    df = df.drop_duplicates(["timestamp", "type", "target"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Repair UTF-8 curly apostrophes that were once decoded as cp1252.
    df["ssid"] = (df["ssid"].str.replace("â€™", "’", regex=False)
                  .str.strip().replace("", NO_SSID))
    df["ok"] = df["success"].str.strip().str.lower().eq("true")
    for c in ["ping_ms", "download_mbps", "upload_mbps"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["hour"] = df["timestamp"].dt.hour
    df["date"] = df["timestamp"].dt.date
    df["kind"] = np.select(
        [df["type"] == "ping", df["type"] == "dns", df["type"].str.startswith("speedtest")],
        ["ping", "dns", "speed"], default="other")
    return df, tz


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def pct(bad, n):
    return 100 * bad / n if n else None


def fmt(v, spec="{:.0f}", unit="", missing="—"):
    return missing if v is None or pd.isna(v) else spec.format(v) + unit


def one_in_n(p):
    return "none" if not p else f"1 in {max(round(100 / p), 1)}"


def humanize(seconds):
    s = int(round(seconds))
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    if s < 172800:
        return f"{s / 3600:.1f} hours".replace(".0 ", " ")
    return f"{s / 86400:.1f} days"


def dt_fmt(ts):
    return ts.strftime("%d %b, %I:%M %p")


def hour_label(h):
    return f"{h:02d}:00–{(h + 1) % 24:02d}:00"


def esc(s):
    return escape(str(s))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def typical_interval(sub):
    """Median gap between consecutive samples of the same target (seconds)."""
    if sub.empty:
        return None
    gaps = sub.groupby("target")["timestamp"].diff().dt.total_seconds().dropna()
    return float(gaps.median()) if len(gaps) else None


def add_cycles(df, cadence):
    """Tag ping rows with a cycle id: rows logged back-to-back form one cycle."""
    p = df[df["kind"] == "ping"].copy()
    brk = p["timestamp"].diff().dt.total_seconds() > cadence * 0.5
    p["cycle"] = brk.cumsum()
    return p


def cycle_table(pings, cadence):
    """One row per ping cycle: start, network, whether every target failed."""
    c = pings.groupby("cycle").agg(start=("timestamp", "min"), ssid=("ssid", "first"),
                                   all_failed=("ok", lambda s: not s.any()))
    c["gap_before"] = c["start"].diff().dt.total_seconds() > cadence * 3
    return c


def find_outages(cycles, cadence):
    """Runs of consecutive cycles where every ping target failed."""
    out, run = [], []
    def close():
        if run:
            first, last = run[0], run[-1]
            out.append({"start": first["start"], "ssid": first["ssid"], "cycles": len(run),
                        "duration": (last["start"] - first["start"]).total_seconds() + cadence})
    for _, row in cycles.iterrows():
        if row["all_failed"] and not (run and row["gap_before"]):
            run.append(row)
        else:
            close(); run = [row] if row["all_failed"] else []
    close()
    return pd.DataFrame(out, columns=["start", "ssid", "cycles", "duration"])


def find_gaps(cycles, cadence):
    """Periods where the logger itself recorded nothing (e.g. PC asleep)."""
    starts = cycles["start"]
    d = starts.diff()
    mask = d.dt.total_seconds() > cadence * 3
    return pd.DataFrame({"start": (starts - d)[mask], "end": starts[mask],
                         "duration": d[mask].dt.total_seconds()})


def network_stats(df, plan_mbps):
    """Per-network metrics. Everything here is measured, nothing is assumed."""
    out = {}
    total_rows = len(df)
    for net, g in df.groupby("ssid"):
        ping, dns, spd = (g[g["kind"] == k] for k in ("ping", "dns", "speed"))
        ok_ping, ok_spd = ping[ping["ok"]], spd[spd["ok"]]
        d = ok_spd["download_mbps"]
        s = {
            "share": 100 * len(g) / total_rows,
            "first": g["timestamp"].min(), "last": g["timestamp"].max(),
            "n_ping": len(ping), "loss": pct((~ping["ok"]).sum(), len(ping)),
            "loss_by_target": {t: pct((~x["ok"]).sum(), len(x)) for t, x in ping.groupby("target")},
            "n_dns": len(dns), "dns_fail": pct((~dns["ok"]).sum(), len(dns)),
            "n_speed": len(spd), "speed_fail": pct((~spd["ok"]).sum(), len(spd)),
            "down_med": d.median() if len(d) else None,
            "down_p10": d.quantile(0.10) if len(d) else None,
            "down_min": d.min() if len(d) else None,
            "down_max": d.max() if len(d) else None,
            "up_med": ok_spd["upload_mbps"].median() if len(ok_spd) else None,
            "lat_med": ok_ping["ping_ms"].median() if len(ok_ping) else None,
            "lat_p95": ok_ping["ping_ms"].quantile(0.95) if len(ok_ping) else None,
            "lat_max": ok_ping["ping_ms"].max() if len(ok_ping) else None,
            "slow_vs_plan": (pct((d < LOW_PLAN_FRACTION * plan_mbps).sum() + len(spd) - len(ok_spd),
                                 len(spd)) if plan_mbps and len(spd) else None),
        }
        out[net] = s
    return out


def badness(s):
    return (s["loss"] or 0) + (s["speed_fail"] or 0)


def verdict(s):
    t = THRESHOLDS
    if s["n_speed"] < t["min_speed_tests"] and s["n_ping"] < t["min_pings"]:
        return "Not enough data", MUT
    fail, loss = s["speed_fail"] or 0, s["loss"] or 0
    if fail >= t["unusable_speed_fail_pct"] or loss >= t["unusable_ping_loss_pct"]:
        return "UNUSABLE", BAD
    if fail >= t["poor_speed_fail_pct"] or loss >= t["poor_ping_loss_pct"]:
        return "POOR", WARN
    return "Good", GOOD


def hourly(df, kind, net, value):
    """Per-hour series (0..23) for one network; NaN where there is no data."""
    g = df[(df["kind"] == kind) & (df["ssid"] == net)]
    if value == "fail_pct":
        s = g.groupby("hour")["ok"].apply(lambda x: 100 * (~x).mean())
    else:
        s = g[g["ok"]].groupby("hour")[value].median()
    return s.reindex(range(24))


def worst_hours(df, net, n=3):
    """Hours whose ping failure rate is clearly above the network's own average."""
    g = df[(df["kind"] == "ping") & (df["ssid"] == net)]
    base = 100 * (~g["ok"]).mean() if len(g) else 0
    by = g.groupby("hour")["ok"].agg(n="size", rate=lambda x: 100 * (~x).mean())
    by = by[(by["n"] >= MIN_HOUR_SAMPLES) & (by["rate"] > max(base * 1.5, 0))]
    return base, by.sort_values("rate", ascending=False).head(n)


def speed_by_period(df, net):
    """Fastest vs slowest hour by median download (needs >=3 tests per hour)."""
    g = df[(df["kind"] == "speed") & (df["ssid"] == net) & df["ok"]]
    by = g.groupby("hour")["download_mbps"].agg(["median", "size"])
    by = by[by["size"] >= 3]
    if len(by) < 2:
        return None
    return by["median"].idxmax(), by["median"].max(), by["median"].idxmin(), by["median"].min()


# ---------------------------------------------------------------------------
# Charts (rendered to memory)
# ---------------------------------------------------------------------------
def _save(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _style_ax(ax, title, ylabel=None):
    ax.set_title(title, fontsize=10, color="#0f1b2d", loc="left")
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8)
    ax.grid(alpha=0.25)
    ax.tick_params(labelsize=8)


def _hour_bars(ax, df, nets, cmap, kind, value, title, ylabel):
    w = 0.8 / len(nets)
    for i, net in enumerate(nets):
        s = hourly(df, kind, net, value)
        ax.bar(np.arange(24) + (i - (len(nets) - 1) / 2) * w, s.fillna(0), w,
               color=cmap[net], alpha=0.85, label=net)
    ax.set_xticks(range(24))
    ax.set_xlabel("Hour of day (24-hour clock)", fontsize=8)
    _style_ax(ax, title, ylabel)
    ax.grid(axis="x", alpha=0)


def make_charts(df, nets, cmap):
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": "#333",
                         "axes.linewidth": 0.8})
    bad = "#c0392b"
    charts = {}
    spd = df[df["kind"] == "speed"]

    # 1. Download speed over time; failed tests plotted as red X on zero.
    fig, ax = plt.subplots(figsize=(9, 3.4))
    for net in nets:
        g = spd[(spd["ssid"] == net) & spd["ok"]]
        ax.plot(g["timestamp"], g["download_mbps"], ".", ms=4, color=cmap[net],
                alpha=0.8, label=net)
        f = spd[(spd["ssid"] == net) & ~spd["ok"]]
        ax.scatter(f["timestamp"], np.zeros(len(f)), marker="x", s=30, color=bad, zorder=5,
                   label="Test failed" if net == nets[0] else None)
    ax.set_ylim(bottom=-0.04 * max(ax.get_ylim()[1], 1))
    _style_ax(ax, "Download speed per test (red X = test failed)", "Mbps (higher is better)")
    ax.legend(fontsize=7, ncol=3)
    fig.autofmt_xdate()
    charts["speed"] = _save(fig)

    # 2. Daily trend: ping drop rate and median download.
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 4.6), sharex=True)
    p = df[df["kind"] == "ping"]
    for net in nets:
        d = p[p["ssid"] == net].groupby("date")["ok"].apply(lambda x: 100 * (~x).mean())
        a1.plot(pd.to_datetime(d.index), d.values, "o-", ms=3, lw=1, color=cmap[net], label=net)
        s = spd[(spd["ssid"] == net) & spd["ok"]].groupby("date")["download_mbps"].median()
        a2.plot(pd.to_datetime(s.index), s.values, "o-", ms=3, lw=1, color=cmap[net], label=net)
    _style_ax(a1, "Daily dropped-ping rate", "% dropped (lower is better)")
    _style_ax(a2, "Daily median download speed", "Mbps (higher is better)")
    a1.legend(fontsize=7, ncol=3)
    a2.set_ylim(bottom=0)
    fig.autofmt_xdate()
    charts["daily"] = _save(fig)

    # 3. Hour-of-day profile (2 x 2).
    fig, axes = plt.subplots(2, 2, figsize=(10, 6))
    _hour_bars(axes[0, 0], df, nets, cmap, "speed", "download_mbps",
               "Typical download by hour", "Mbps (higher is better)")
    _hour_bars(axes[0, 1], df, nets, cmap, "speed", "fail_pct",
               "Failed speed tests by hour", "% failed (lower is better)")
    _hour_bars(axes[1, 0], df, nets, cmap, "ping", "fail_pct",
               "Dropped pings by hour", "% dropped (lower is better)")
    _hour_bars(axes[1, 1], df, nets, cmap, "dns", "fail_pct",
               "Failed DNS lookups by hour", "% failed (lower is better)")
    axes[0, 0].legend(fontsize=7)
    for a in axes.flat:
        a.tick_params(axis="x", labelsize=6)
    fig.tight_layout()
    charts["hourly"] = _save(fig)

    # 4. Latency distribution per network (successful pings only).
    data = [p[(p["ssid"] == n) & p["ok"]]["ping_ms"].dropna() for n in nets]
    if any(len(d) for d in data):
        fig, ax = plt.subplots(figsize=(9, 0.9 + 0.7 * len(nets)))
        bp = ax.boxplot([d if len(d) else [np.nan] for d in data], orientation="horizontal",
                        whis=(5, 95), showfliers=False, patch_artist=True,
                        tick_labels=nets)
        for patch, n in zip(bp["boxes"], nets):
            patch.set(facecolor=cmap[n], alpha=0.6)
        ax.set_xscale("log")
        ax.set_xlabel("Round-trip time (ms, log scale). Box = middle 50%, whiskers = 5th–95th percentile",
                      fontsize=8)
        _style_ax(ax, "Response delay per network")
        charts["latency"] = _save(fig)
    return charts


# ---------------------------------------------------------------------------
# Plain-English findings, generated from the numbers
# ---------------------------------------------------------------------------
def network_summary(net, s):
    label, col = verdict(s)
    parts = []
    if s["speed_fail"]:
        parts.append(f"<b>{s['speed_fail']:.0f}%</b> of speed tests failed outright "
                     f"({one_in_n(s['speed_fail'])})")
    if s["loss"]:
        parts.append(f"<b>{s['loss']:.1f}%</b> of connectivity pings got no reply "
                     f"({one_in_n(s['loss'])})")
    if s["dns_fail"]:
        parts.append(f"<b>{s['dns_fail']:.1f}%</b> of website name lookups failed")
    if s["down_med"] is not None:
        parts.append(f"median download <b>{s['down_med']:.0f} Mbps</b> "
                     f"(slowest 10% of tests below {s['down_p10']:.0f}, worst {s['down_min']:.0f})")
    if s["lat_med"] is not None:
        parts.append(f"response delay typically {s['lat_med']:.0f} ms, "
                     f"slowest 5% above {s['lat_p95']:.0f} ms, peak {s['lat_max']:.0f} ms")
    if s["slow_vs_plan"] is not None:
        parts.append(f"<b>{s['slow_vs_plan']:.0f}%</b> of tests failed or came in under "
                     f"{LOW_PLAN_FRACTION:.0%} of the plan speed")
    body = "; ".join(parts) if parts else "no problems detected in the recorded data"
    return (f'<b>{esc(net)}</b> — <b><font color="{col.hexval()}">{label}</font></b>: '
            f"{body}.")


def findings(df, nets, stats, outages, gaps, span_s):
    """Bullet strings (HTML) derived from the data. Nothing is asserted about cause."""
    out = [network_summary(n, stats[n]) for n in sorted(nets, key=lambda n: -badness(stats[n]))]

    if len(nets) >= 2:
        ranked = sorted(nets, key=lambda n: badness(stats[n]))
        best, worst = ranked[0], ranked[-1]
        bl, wl = stats[best]["loss"], stats[worst]["loss"]
        if bl and wl and wl > bl:
            out.append(f"<b>{esc(worst)}</b> dropped pings {wl / bl:.1f}× as often as "
                       f"<b>{esc(best)}</b> ({wl:.1f}% vs {bl:.1f}%).")

    if len(outages):
        longest = outages.loc[outages["duration"].idxmax()]
        out.append(
            f"<b>{len(outages)}</b> complete outages were recorded (every ping target "
            f"unreachable), totalling <b>{humanize(outages['duration'].sum())}</b>. "
            f"The longest lasted <b>{humanize(longest['duration'])}</b>, starting "
            f"{dt_fmt(longest['start'])} on {esc(longest['ssid'])}.")
    else:
        out.append("No complete outages (all ping targets unreachable at once) were recorded.")

    for net in nets:
        base, hrs = worst_hours(df, net)
        if len(hrs):
            hs = ", ".join(f"{hour_label(h)} ({r.rate:.1f}%)" for h, r in hrs.iterrows())
            out.append(f"On <b>{esc(net)}</b>, pings dropped most often at {hs}, against an "
                       f"overall average of {base:.1f}%.")
        sp = speed_by_period(df, net)
        if sp and sp[1] > 0 and sp[3] < 0.8 * sp[1]:
            out.append(f"On <b>{esc(net)}</b>, median download ranged from {sp[3]:.0f} Mbps "
                       f"around {hour_label(sp[2])} to {sp[1]:.0f} Mbps around {hour_label(sp[0])}.")

    if len(gaps):
        out.append(f"The monitor itself recorded nothing for <b>{humanize(gaps['duration'].sum())}</b> "
                   f"across {len(gaps)} gaps ({100 * gaps['duration'].sum() / span_s:.0f}% of the "
                   f"period). Those periods are not counted as outages.")
    return out


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def build_pdf(df, nets, stats, charts, outages, gaps, cadences, plan_mbps, out_pdf):
    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Title"], textColor=INK, fontSize=20,
                        spaceAfter=4, alignment=TA_LEFT)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], textColor=MUT, fontSize=10, spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=INK, fontSize=13,
                        spaceBefore=12, spaceAfter=6)
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=10, leading=14,
                          spaceAfter=6, textColor=INK)
    SMALL = ParagraphStyle("SMALL", parent=ss["Normal"], fontSize=8, textColor=MUT, leading=11)
    CAP = ParagraphStyle("CAP", parent=ss["Normal"], fontSize=8.5, textColor=MUT,
                         alignment=TA_CENTER, spaceAfter=10, spaceBefore=2, leading=12)
    HDR = ParagraphStyle("HDR", fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.white)
    CELL = ParagraphStyle("CELL", fontSize=8.5, leading=10.5, alignment=TA_CENTER)
    NAME = ParagraphStyle("NAME", fontSize=8.5, leading=10.5)

    start, end = df["timestamp"].min(), df["timestamp"].max()
    span_s = max((end - start).total_seconds(), 1)
    story = []

    story.append(Paragraph("Internet Service Quality Report", H1))
    story.append(Paragraph(
        f"{len(df):,} automatic tests from {dt_fmt(start)} {start.year} to "
        f"{dt_fmt(end)} {end.year} ({humanize(span_s)}), covering "
        f"{len(stats)} network{'s' if len(stats) != 1 else ''}.", SUB))

    story.append(Paragraph("The bottom line", H2))
    for line in findings(df, nets, stats, outages, gaps, span_s):
        story.append(Paragraph("•&nbsp;&nbsp;" + line, BODY))

    # ---- Scorecard ----
    story.append(Paragraph("Scorecard: every network seen", H2))
    header = ["Network", "Verdict", "Share of\ndata", "Speed tests\nfailed", "Pings\ndropped",
              "DNS lookups\nfailed", "Median\ndownload", "Worst\ndownload", "Median\ndelay"]
    rows = [[Paragraph(f"<b>{h}</b>".replace("\n", "<br/>"), HDR) for h in header]]

    def c(txt, color=INK):
        return Paragraph(f'<font color="{color.hexval()}">{txt}</font>', CELL)

    for net, s in sorted(stats.items(), key=lambda kv: -badness(kv[1])):
        label, col = verdict(s)
        rows.append([
            Paragraph(f"<b>{esc(net)}</b>", NAME), c(f"<b>{label}</b>", col),
            c(fmt(s["share"], "{:.0f}", "%")),
            c(fmt(s["speed_fail"], "{:.0f}", "%"), BAD if (s["speed_fail"] or 0) >= THRESHOLDS["poor_speed_fail_pct"] else INK),
            c(fmt(s["loss"], "{:.1f}", "%"), BAD if (s["loss"] or 0) >= THRESHOLDS["poor_ping_loss_pct"] else INK),
            c(fmt(s["dns_fail"], "{:.1f}", "%")),
            c(fmt(s["down_med"], "{:.0f}", " Mbps")), c(fmt(s["down_min"], "{:.0f}", " Mbps")),
            c(fmt(s["lat_med"], "{:.0f}", " ms")),
        ])
    tbl = Table(rows, colWidths=[3.2 * cm, 2.1 * cm, 1.4 * cm, 1.9 * cm, 1.6 * cm,
                                 1.9 * cm, 1.9 * cm, 1.9 * cm, 1.5 * cm], repeatRows=1)
    tbl.setStyle(TABLE_STYLE)
    story.append(tbl)
    t = THRESHOLDS
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "<b>How verdicts are assigned.</b> "
        f"UNUSABLE: at least {t['unusable_speed_fail_pct']}% of speed tests failed or at least "
        f"{t['unusable_ping_loss_pct']}% of pings dropped. POOR: at least {t['poor_speed_fail_pct']}% "
        f"of speed tests failed or at least {t['poor_ping_loss_pct']}% of pings dropped. "
        f"A network needs {t['min_speed_tests']}+ speed tests or {t['min_pings']}+ pings to be "
        "judged. 'Pings dropped' = a ping got no reply; 'DNS lookups failed' = a website name could "
        "not be translated to an address; 'Speed tests failed' = the test could not complete.", SMALL))

    # ---- Detail per network ----
    story.append(Paragraph("Detail per network", H2))
    dh = ["Network", "Active\nbetween", "Pings\n(n)", "Speed tests\n(n)", "Upload\nmedian",
          "Slowest 10%\nof downloads", "Delay 95th\npercentile", "Peak\ndelay"]
    if plan_mbps:
        dh.append(f"Under {LOW_PLAN_FRACTION:.0%} of\n{plan_mbps:g} Mbps plan")
    drows = [[Paragraph(f"<b>{h}</b>".replace("\n", "<br/>"), HDR) for h in dh]]
    for net, s in sorted(stats.items(), key=lambda kv: -badness(kv[1])):
        r = [Paragraph(f"<b>{esc(net)}</b>", NAME),
             c(f"{s['first']:%d %b}–{s['last']:%d %b}"),
             c(f"{s['n_ping']:,}"), c(f"{s['n_speed']:,}"),
             c(fmt(s["up_med"], "{:.0f}", " Mbps")), c(fmt(s["down_p10"], "{:.0f}", " Mbps")),
             c(fmt(s["lat_p95"], "{:.0f}", " ms")), c(fmt(s["lat_max"], "{:.0f}", " ms"))]
        if plan_mbps:
            r.append(c(fmt(s["slow_vs_plan"], "{:.0f}", "%")))
        drows.append(r)
    ncol = len(dh)
    first_w = 3.4 * cm
    other = (17.4 * cm - first_w) / (ncol - 1)
    dt = Table(drows, colWidths=[first_w] + [other] * (ncol - 1), repeatRows=1)
    dt.setStyle(TABLE_STYLE)
    story.append(dt)

    # ---- Outages ----
    story.append(Paragraph("Complete outages", H2))
    if len(outages):
        top = outages.sort_values("duration", ascending=False).head(10)
        orows = [[Paragraph(f"<b>{h}</b>", HDR) for h in ["Started", "Network", "Lasted", "Checks missed"]]]
        for _, o in top.iterrows():
            orows.append([c(dt_fmt(o["start"])), Paragraph(esc(o["ssid"]), NAME),
                          c(humanize(o["duration"])), c(str(o["cycles"]))])
        ot = Table(orows, colWidths=[4.5 * cm, 5.5 * cm, 3.5 * cm, 3.5 * cm], repeatRows=1)
        ot.setStyle(TABLE_STYLE)
        story.append(Paragraph(
            f"An outage is a run of consecutive checks in which <i>every</i> ping target failed. "
            f"Showing the {len(top)} longest of {len(outages)}.", BODY))
        story.append(ot)
    else:
        story.append(Paragraph("None recorded.", BODY))

    story.append(PageBreak())

    # ---- Charts ----
    def chart(key, title, caption, width=17.4 * cm):
        if key not in charts:
            return
        img = Image(charts[key], width=width)
        ratio = img.imageHeight / img.imageWidth
        story.append(KeepTogether([Paragraph(title, H2),
                                   Image(charts[key], width=width, height=width * ratio),
                                   Paragraph(caption, CAP)]))

    chart("speed", "1. Speed over time",
          "Each dot is one speed test; failed tests sit at zero so they stay visible.")
    chart("daily", "2. Day-by-day trend",
          "Daily ping drop rate and median download for each compared network. Days with no "
          "data for a network are simply absent.")
    story.append(PageBreak())
    chart("hourly", "3. Time-of-day profile",
          "Each bar is one hour of the day, pooled across all days. Hours with few samples can "
          "look extreme; the counts per network are in the detail table.")
    chart("latency", "4. Response delay",
          "How long a reply takes. A wide box or long right-hand whisker means an unsteady "
          "connection even if the average looks fine.")

    # ---- Method (all values read from the log) ----
    story.append(Paragraph("How this was measured", H2))
    targets = lambda k: ", ".join(sorted(df[df["kind"] == k]["target"].replace("", np.nan).dropna().unique())) or "n/a"
    kinds = df["kind"].value_counts()
    bits = []
    if cadences.get("ping"):
        bits.append(f"every ~{humanize(cadences['ping'])} the monitor pinged {esc(targets('ping'))}")
    if cadences.get("dns"):
        bits.append(f"looked up {esc(targets('dns'))} by name")
    if cadences.get("speed"):
        bits.append(f"every ~{humanize(cadences['speed'])} ran a full speed test")
    story.append(Paragraph(
        "A monitoring program logged the connection automatically: " + "; ".join(bits) +
        ". It also noted which WiFi network was in use, so each network is scored separately. "
        f"This report is computed from {kinds.get('ping', 0):,} ping, {kinds.get('dns', 0):,} "
        f"DNS and {kinds.get('speed', 0):,} speed-test records. Charts compare: "
        f"{esc(', '.join(nets))}.", BODY))
    story.append(Paragraph(
        "Notes: a failed speed test is counted as a failure rather than a slow result. Ping and "
        "DNS checks use public servers, so a failure means either the local connection or the "
        "path beyond it; the log alone cannot say which. Times are in the log's local timezone.",
        SMALL))

    SimpleDocTemplate(out_pdf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                      leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                      title="Internet Service Quality Report").build(story)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def pick_compared(stats, requested):
    """Networks for the charts: --compare list, else every named network with enough data."""
    if requested:
        found = [n for n in requested if n in stats]
        for n in requested:
            if n not in stats:
                print(f"WARNING: '{n}' not in the log. Networks present: {', '.join(stats)}")
        if found:
            return found
        print("No requested network found; auto-selecting.")
    t = THRESHOLDS
    enough = [n for n, s in stats.items() if n != NO_SSID
              and (s["n_ping"] >= t["min_pings"] or s["n_speed"] >= t["min_speed_tests"])]
    return enough or [n for n in stats if n != NO_SSID] or list(stats)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv", nargs="*", help="log file(s); default ../output/network_log.csv")
    ap.add_argument("-o", "--output", help="PDF path; default internet_report.pdf next to the first CSV")
    ap.add_argument("--compare", help="comma-separated network names to chart (default: auto)")
    ap.add_argument("--plan-mbps", type=float, help="advertised download speed, to judge tests against it")
    args = ap.parse_args()

    paths = args.csv or [os.path.join(os.path.dirname(here), "output", "network_log.csv")]
    for p in paths:
        if not os.path.exists(p):
            sys.exit(f"ERROR: log file not found: {p}")
    out_pdf = args.output or os.path.join(os.path.dirname(os.path.abspath(paths[0])),
                                          "internet_report.pdf")

    print(f"Reading {', '.join(paths)} ...")
    df, _ = load_logs(paths)
    if df.empty:
        sys.exit("ERROR: no usable rows found in the log.")

    cadences = {k: typical_interval(df[df["kind"] == k]) for k in ("ping", "dns", "speed")}
    if not cadences["ping"]:
        sys.exit("ERROR: the log has no repeated ping records to analyse.")
    pings = add_cycles(df, cadences["ping"])
    cycles = cycle_table(pings, cadences["ping"])
    outages = find_outages(cycles, cadences["ping"])
    gaps = find_gaps(cycles, cadences["ping"])

    stats = network_stats(df, args.plan_mbps)
    req = [x.strip() for x in args.compare.split(",") if x.strip()] if args.compare else None
    nets = pick_compared(stats, req)
    cmap = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(nets)}
    print(f"Comparing in charts: {', '.join(nets)}")

    charts = make_charts(df, nets, cmap)
    build_pdf(df, nets, stats, charts, outages, gaps, cadences, args.plan_mbps, out_pdf)
    print(f"Done. Report written to: {out_pdf}")


if __name__ == "__main__":
    main()
