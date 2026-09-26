"""
Build a plain-English internet quality report (PDF) from a network_log.csv.

Usage:
    python make_report.py                          # uses ../output/network_log.csv
    python make_report.py path/to/network_log.csv  # uses a specific file
    python make_report.py mylog.csv report.pdf     # custom output name

Everything in the report is computed from the data — networks, dates, and
numbers are all dynamic, so you can run this again whenever you collect more
data. No values are hardcoded.
"""

import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                Table, TableStyle, PageBreak, KeepTogether)

# ---------------------------------------------------------------------------
# Colours / styles
# ---------------------------------------------------------------------------
INK = colors.HexColor("#0f1b2d")
BAD = colors.HexColor("#c0392b")
WARN = colors.HexColor("#c77d0a")
GOOD = colors.HexColor("#2f7d4f")
MUT = colors.HexColor("#6b6355")
LINE = colors.HexColor("#d9d4c6")
LIGHT = colors.HexColor("#f5f3ec")

# The columns this tool understands. Old logs have 8, newer logs add "ssid".
BASE_COLS = ["timestamp", "type", "target", "ping_ms", "packet_loss_pct",
             "download_mbps", "upload_mbps", "success"]


# ---------------------------------------------------------------------------
# Load & clean — tolerant of the mid-file schema change
# ---------------------------------------------------------------------------
def load_log(path):
    """Read the CSV even if some rows have an ssid column and some don't."""
    # Read every row as up to 9 fields, no header assumptions.
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f):
            parts = raw.rstrip("\n\r").split(",")
            if i == 0 and parts[0] == "timestamp":
                continue  # skip header
            if len(parts) < 8:
                continue  # malformed, skip
            row = parts[:9]
            if len(row) == 8:
                row.append("")  # no ssid recorded on this row
            rows.append(row)

    df = pd.DataFrame(rows, columns=BASE_COLS + ["ssid"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"])
    # Local time — use the machine's local zone offset already in the data.
    df["timestamp"] = df["timestamp"].dt.tz_convert(
        df["timestamp"].dt.tz if df["timestamp"].dt.tz else "UTC")

    # Fix mojibake in SSID names (curly apostrophe etc.) and blanks.
    df["ssid"] = (df["ssid"].astype(str)
                  .str.replace("\u00e2\u20ac\u2122", "'", regex=False)
                  .replace({"": "(unknown network)", "nan": "(unknown network)"}))
    df["success_bool"] = df["success"].astype(str).str.strip().eq("True")
    for c in ["ping_ms", "download_mbps", "upload_mbps"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["hour"] = df["timestamp"].dt.hour
    df["date"] = df["timestamp"].dt.date
    return df


# ---------------------------------------------------------------------------
# Per-network statistics
# ---------------------------------------------------------------------------
def network_stats(df):
    """Return a dict of stats per network, sorted worst-first."""
    out = {}
    for net, g in df.groupby("ssid"):
        pings = g[g["type"] == "ping"]
        dns = g[g["type"] == "dns"]
        speed = g[g["type"].str.startswith("speedtest")]
        ok = speed[speed["success_bool"]]
        ok_ping = pings[pings["success_bool"]]

        n_ping = len(pings)
        n_speed = len(speed)
        out[net] = {
            "n_ping": n_ping,
            "loss_pct": 100 * (~pings["success_bool"]).sum() / n_ping if n_ping else 0,
            "n_speed": n_speed,
            "speed_fail_pct": 100 * (~speed["success_bool"]).sum() / n_speed if n_speed else 0,
            "down_median": ok["download_mbps"].median() if len(ok) else None,
            "down_min": ok["download_mbps"].min() if len(ok) else None,
            "down_max": ok["download_mbps"].max() if len(ok) else None,
            "up_median": ok["upload_mbps"].median() if len(ok) else None,
            "ping_median": ok_ping["ping_ms"].median() if len(ok_ping) else None,
            "ping_max": ok_ping["ping_ms"].max() if len(ok_ping) else None,
            "n_dns": len(dns),
            "dns_fail_pct": 100 * (~dns["success_bool"]).sum() / len(dns) if len(dns) else 0,
        }
    # Sort so the worst network (highest combined failure) comes first.
    def badness(s):
        return s["loss_pct"] + s["speed_fail_pct"]
    return dict(sorted(out.items(), key=lambda kv: badness(kv[1]), reverse=True))


def verdict(s):
    """Plain-English verdict for one network: (label, colour)."""
    loss = s["loss_pct"]
    fail = s["speed_fail_pct"]
    if s["n_speed"] < 3 and s["n_ping"] < 50:
        return ("Not enough data", MUT)
    if fail >= 20 or loss >= 5:
        return ("UNUSABLE", BAD)
    if fail >= 5 or loss >= 1:
        return ("POOR", WARN)
    return ("Good", GOOD)


def one_in_n(pct):
    """Turn a percentage into '1 in N' language a layperson understands."""
    if pct <= 0:
        return "none"
    n = round(100 / pct)
    return f"1 in {max(n,1)}"


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def _mpl(color):
    """reportlab Color -> matplotlib '#rrggbb' string."""
    return "#{:02x}{:02x}{:02x}".format(
        int(color.red * 255), int(color.green * 255), int(color.blue * 255))


def make_charts(df, stats, charted, outdir):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "figure.dpi": 150, "axes.edgecolor": "#333",
                         "axes.linewidth": 0.8})
    BAD_HEX = _mpl(BAD)
    # Assign a colour per compared network.
    palette = ["#1f6feb", "#b5651d", "#2f7d4f", "#8e44ad", "#16a085"]
    cmap = {net: palette[i % len(palette)] for i, net in enumerate(charted)}
    hours = list(range(24))
    w = 0.8 / max(len(charted), 1)
    paths = {}

    def break_gaps(g, col, gap="2h"):
        g = g.sort_values("timestamp").copy()
        g.loc[g["timestamp"].diff() > pd.Timedelta(gap), col] = np.nan
        return g

    st = df[df["type"].str.startswith("speedtest")]
    p_all = df[df["type"] == "ping"]

    # Chart 1: speed over time, failures as red X at zero (all compared nets)
    fig, ax = plt.subplots(figsize=(9, 3.6))
    for net in charted:
        g = break_gaps(st[st["ssid"] == net], "download_mbps")
        ok = g[g["success_bool"] | g["download_mbps"].isna()]
        ax.plot(ok["timestamp"], ok["download_mbps"], ".-", ms=4, lw=0.8,
                color=cmap[net], alpha=0.85, label=net)
        f = st[(st["ssid"] == net) & ~st["success_bool"]]
        ax.scatter(f["timestamp"], [0] * len(f), marker="x", s=40, color=BAD_HEX,
                   zorder=5, label="Failed (no internet)" if net == charted[0] else None)
    ax.axhline(0, color=BAD_HEX, lw=0.5, ls=":")
    ax.set_ylabel("Download speed (Mbps)\nhigher is better")
    ax.set_title("Internet speed over time — red X = the connection failed completely",
                 fontsize=10, color="#0f1b2d")
    ax.legend(fontsize=7, loc="upper right", ncol=2)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    plt.tight_layout(); p = os.path.join(outdir, "chart_speed.png")
    plt.savefig(p, bbox_inches="tight"); plt.close(); paths["speed"] = p

    # Chart 2: median speed by hour of day — comparative (shows night-time drop)
    fig, ax = plt.subplots(figsize=(9, 3.4))
    for i, net in enumerate(charted):
        g = st[(st["ssid"] == net) & st["success_bool"]]
        meds = []
        for hr in hours:
            h = g[g["hour"] == hr]["download_mbps"]
            meds.append(h.median() if len(h) else 0)
        ax.bar([hr + (i - (len(charted) - 1) / 2) * w for hr in hours], meds, w,
               color=cmap[net], alpha=0.85, label=net)
    ax.set_xlabel("Time of day (24-hour clock)")
    ax.set_ylabel("Typical download (Mbps)\nhigher is better")
    ax.set_title("Typical internet speed by time of day", fontsize=10, color="#0f1b2d")
    ax.set_xticks(hours); ax.set_xticklabels(hours, fontsize=7)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.25)
    plt.tight_layout(); p = os.path.join(outdir, "chart_speedhour.png")
    plt.savefig(p, bbox_inches="tight"); plt.close(); paths["speedhour"] = p

    # Chart 3: speed-test failure rate by hour — comparative
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for i, net in enumerate(charted):
        g = st[st["ssid"] == net]
        rates = []
        for hr in hours:
            h = g[g["hour"] == hr]
            rates.append(100 * (~h["success_bool"]).sum() / len(h) if len(h) else 0)
        ax.bar([hr + (i - (len(charted) - 1) / 2) * w for hr in hours], rates, w,
               color=cmap[net], alpha=0.85, label=net)
    ax.set_xlabel("Time of day (24-hour clock)")
    ax.set_ylabel("% of tests that failed\nlower is better")
    ax.set_title("When the internet failed completely, by time of day",
                 fontsize=10, color="#0f1b2d")
    ax.set_xticks(hours); ax.set_xticklabels(hours, fontsize=7)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.25)
    plt.tight_layout(); p = os.path.join(outdir, "chart_failhour.png")
    plt.savefig(p, bbox_inches="tight"); plt.close(); paths["failhour"] = p

    # Chart 4: dropped connections by hour (packet loss) — comparative
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for i, net in enumerate(charted):
        g = p_all[p_all["ssid"] == net]
        rates = []
        for hr in hours:
            h = g[g["hour"] == hr]
            rates.append(100 * (~h["success_bool"]).sum() / len(h) if len(h) else 0)
        ax.bar([hr + (i - (len(charted) - 1) / 2) * w for hr in hours], rates, w,
               color=cmap[net], alpha=0.85, label=net)
    ax.set_xlabel("Time of day (24-hour clock)")
    ax.set_ylabel("% of connection checks\nthat dropped — lower is better")
    ax.set_title("Dropped connections by time of day", fontsize=10, color="#0f1b2d")
    ax.set_xticks(hours); ax.set_xticklabels(hours, fontsize=7)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.25)
    plt.tight_layout(); p = os.path.join(outdir, "chart_losshour.png")
    plt.savefig(p, bbox_inches="tight"); plt.close(); paths["losshour"] = p

    return paths


# ---------------------------------------------------------------------------
# Choose which networks to compare
# ---------------------------------------------------------------------------
def is_hotspot(name):
    """Heuristic: does this network name look like a phone hotspot?"""
    low = name.lower()
    return any(k in low for k in ["iphone", "hotspot", "phone", "galaxy", "pixel",
                                  "android", "'s ", "\u2019s "])


def pick_compared(stats, requested):
    """Decide which networks go in the comparison charts.

    requested: list of names from --compare, or None to auto-pick.
    Auto-pick = every real named network with enough data (excludes the
    unknown-network artifact). Table 1 always shows all networks regardless.
    """
    all_nets = list(stats.keys())
    if requested:
        chosen = []
        missing = []
        for name in requested:
            if name in stats:
                chosen.append(name)
            else:
                missing.append(name)
        if missing:
            print("WARNING: these --compare names were not found in the log and "
                  "will be skipped:")
            for m in missing:
                print(f"    {m}")
            print("Networks actually present:")
            for n in all_nets:
                print(f"    {n}")
        if not chosen:
            print("ERROR: none of the requested networks were found. "
                  "Falling back to auto-pick.")
        else:
            return chosen
    # Auto-pick: real named WiFi networks with enough data, excluding phone
    # hotspots (a hotspot is a fallback, not the service being complained about).
    auto = [n for n in all_nets
            if n != "(unknown network)"
            and not is_hotspot(n)
            and (stats[n]["n_ping"] >= 30 or stats[n]["n_speed"] >= 3)]
    if not auto:
        # Nothing left after excluding hotspots — fall back to any named network.
        auto = [n for n in all_nets if n != "(unknown network)"] or all_nets
    return auto


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def build_pdf(df, stats, charts, charted, out_pdf):
    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Title"], textColor=INK, fontSize=20,
                        spaceAfter=4, alignment=TA_LEFT)
    SUB = ParagraphStyle("SUB", parent=styles["Normal"], textColor=MUT, fontSize=10,
                         spaceAfter=14)
    H2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=INK, fontSize=13,
                        spaceBefore=14, spaceAfter=6)
    BODY = ParagraphStyle("BODY", parent=styles["Normal"], fontSize=10.5, leading=15,
                          spaceAfter=8, textColor=INK)
    BIG = ParagraphStyle("BIG", parent=styles["Normal"], fontSize=13, leading=18,
                         spaceAfter=8, textColor=INK)
    SMALL = ParagraphStyle("SMALL", parent=styles["Normal"], fontSize=8, textColor=MUT,
                           leading=11)
    CAP = ParagraphStyle("CAP", parent=styles["Normal"], fontSize=8.5, textColor=MUT,
                         alignment=TA_CENTER, spaceAfter=12, spaceBefore=2, leading=12)

    start = df["timestamp"].min()
    end = df["timestamp"].max()
    days = (end - start).days + 1

    story = []
    story.append(Paragraph("Internet Service Quality Report", H1))
    story.append(Paragraph(
        f"Based on {len(df):,} automatic tests between "
        f"{start.strftime('%d %b %Y, %I:%M %p')} and {end.strftime('%d %b %Y, %I:%M %p')} "
        f"({days} days).", SUB))

    # ---- The plain-English verdict box, worst COMPARED network first ----
    story.append(Paragraph("The bottom line", H2))
    # Among the networks being compared, lead with the worst one.
    worst = max(charted, key=lambda n: stats[n]["loss_pct"] + stats[n]["speed_fail_pct"]) \
        if charted else list(stats.keys())[0]
    ws = stats[worst]
    wlabel, wcol = verdict(ws)
    story.append(Paragraph(
        f'On <b>{worst}</b>, the internet was <b><font color="{wcol.hexval()}">'
        f'{wlabel}</font></b>.', BIG))

    lines = []
    if ws["speed_fail_pct"] > 0:
        lines.append(
            f"The internet failed completely during <b>{ws['speed_fail_pct']:.0f}% "
            f"of speed tests</b> — that's about {one_in_n(ws['speed_fail_pct'])} tests "
            f"where the connection simply did not work.")
    if ws["loss_pct"] > 0:
        lines.append(
            f"About <b>{one_in_n(ws['loss_pct'])} connection checks were dropped</b> "
            f"({ws['loss_pct']:.1f}%). Dropped connections are what make video calls "
            f"freeze and pages stop loading.")
    if ws["down_median"] is not None and ws["down_min"] is not None:
        lines.append(
            f"Typical download speed was <b>{ws['down_median']:.0f} Mbps</b>, but it "
            f"dropped as low as <b>{ws['down_min']:.0f} Mbps</b> at its worst.")
    if ws["ping_max"] is not None and ws["ping_median"] is not None:
        lines.append(
            f"Delay (how long the internet takes to respond) was usually "
            f"{ws['ping_median']:.0f} milliseconds, but spiked to "
            f"<b>{ws['ping_max']:.0f} milliseconds</b> — high enough to break calls "
            f"and games.")
    for ln in lines:
        story.append(Paragraph("•&nbsp;&nbsp;" + ln, BODY))

    # ---- Simple scorecard table for every network ----
    story.append(Paragraph("How each connection scored", H2))
    story.append(Paragraph(
        "Every WiFi network and phone hotspot that was connected during the test period "
        "is listed below. Green is good, orange is poor, red means the connection was "
        "effectively unusable. The detailed charts that follow focus on: "
        f"<b>{', '.join(charted)}</b>.",
        BODY))

    header = ["Connection", "Verdict", "Failed\ncompletely", "Dropped\nconnections",
              "Typical\nspeed", "Worst\nspeed"]
    tstyle_rows = []
    data = [[Paragraph(f'<b>{h}</b>', ParagraphStyle("h", fontSize=8, leading=10,
            alignment=TA_CENTER, textColor=colors.white)) for h in header]]
    for net in stats:
        s = stats[net]
        label, col = verdict(s)
        def c(txt, color=INK):
            return Paragraph(f'<font color="{color.hexval()}">{txt}</font>',
                             ParagraphStyle("c", fontSize=9, leading=11, alignment=TA_CENTER))
        down_med = f"{s['down_median']:.0f} Mbps" if s["down_median"] is not None else "—"
        down_min = f"{s['down_min']:.0f} Mbps" if s["down_min"] is not None else "—"
        data.append([
            Paragraph(f"<b>{net}</b>", ParagraphStyle("n", fontSize=8.5, leading=10)),
            c(label, col),
            c(f"{s['speed_fail_pct']:.0f}%", BAD if s["speed_fail_pct"] >= 5 else INK),
            c(f"{s['loss_pct']:.1f}%", BAD if s["loss_pct"] >= 1 else INK),
            c(down_med),
            c(down_min, BAD if (s["down_min"] is not None and s["down_min"] < 5) else INK),
        ])
    tbl = Table(data, colWidths=[3.6 * cm, 2.4 * cm, 2.3 * cm, 2.5 * cm, 2.2 * cm, 2.2 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        '"Failed completely" = the connection could not run a basic speed test at all. '
        '"Dropped connections" = the share of quick connection checks that got no reply. '
        "A healthy home internet connection drops well under 1 in 100.", SMALL))

    story.append(PageBreak())

    # ---- Charts with plain captions ----
    def chart(key, title, caption, h=6.5 * cm):
        story.append(KeepTogether([
            Paragraph(title, H2),
            Image(charts[key], width=17 * cm, height=h),
            Paragraph(caption, CAP),
        ]))

    # ---- Charts with plain captions, grouped: speed first, reliability second ----
    def chart(key, title, caption, h=6.5 * cm):
        story.append(KeepTogether([
            Paragraph(title, H2),
            Image(charts[key], width=17 * cm, height=h),
            Paragraph(caption, CAP),
        ]))

    story.append(Paragraph("Part A — How fast was the connection?", H2))
    chart("speed", "1. Speed over time",
          "Each dot is one speed test, with a separate line per network. A red X marks a "
          "test that failed completely (no internet at all); these sit on the zero line so "
          "they stay visible.")
    chart("speedhour", "2. Typical speed by time of day",
          "The typical (median) download speed at each hour, comparing the networks side by "
          "side. Lower bars in the evening and night show the connection slowing down when "
          "it should be usable.")
    story.append(PageBreak())
    story.append(Paragraph("Part B — How often did it fail?", H2))
    chart("failhour", "3. Complete failures by time of day",
          "The share of speed tests that failed completely at each hour. Tall bars mean the "
          "internet often did not work at all at that time of day.")
    chart("losshour", "4. Dropped connections by time of day",
          "How often quick connection checks got no reply, hour by hour. Even short drops "
          "freeze calls and buffer video. A healthy network stays near zero.")

    # ---- Methodology ----
    story.append(Paragraph("How this was measured", H2))
    story.append(Paragraph(
        "A monitoring program ran automatically on a laptop and tested the connection "
        "around the clock. Every 90 seconds it checked whether the internet responded "
        "and how fast it replied; every 30 minutes it ran a full speed test; and it "
        "noted which WiFi network was in use each time, so each connection is scored "
        "separately. All the numbers in this report are calculated directly from those "
        f"{len(df):,} recorded tests. The raw data is available as a spreadsheet on request.",
        BODY))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "For the technically inclined: 'dropped connections' is ICMP packet loss to "
        "8.8.8.8 and 1.1.1.1; 'delay' is round-trip latency; 'failed completely' counts "
        "Ookla Speedtest runs that did not return a result.", SMALL))

    SimpleDocTemplate(out_pdf, pagesize=A4, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                      leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                      title="Internet Service Quality Report").build(story)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv):
    """Separate the --compare flag from positional args (csv, out_pdf)."""
    compare = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--compare":
            if i + 1 < len(argv):
                compare = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
                i += 2
                continue
            else:
                print("ERROR: --compare needs a comma-separated list of network names.")
                sys.exit(1)
        elif a.startswith("--compare="):
            compare = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
        else:
            positional.append(a)
        i += 1
    return positional, compare


def main():
    positional, compare = parse_args(sys.argv[1:])
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_csv = os.path.join(os.path.dirname(script_dir), "output", "network_log.csv")

    csv_path = positional[0] if len(positional) >= 1 else default_csv
    if not os.path.exists(csv_path):
        print(f"ERROR: could not find the log file: {csv_path}")
        print("Pass the path to your network_log.csv, e.g.:")
        print("    python make_report.py ../output/network_log.csv")
        print("    python make_report.py ../output/network_log.csv report.pdf "
              "--compare HarryPotter_5G,HarryPotter_2G")
        sys.exit(1)

    out_pdf = positional[1] if len(positional) >= 2 else os.path.join(
        os.path.dirname(csv_path), "internet_report.pdf")

    print(f"Reading {csv_path} ...")
    df = load_log(csv_path)
    if len(df) == 0:
        print("ERROR: no usable rows found in the log.")
        sys.exit(1)

    stats = network_stats(df)
    # The unknown-network rows (logged before SSID tracking, or during a full
    # outage) are a measurement artifact, not a network to report on. Drop them
    # from the scorecard as long as real named networks exist.
    named = {k: v for k, v in stats.items() if k != "(unknown network)"}
    if named:
        stats = named

    # Decide which networks the comparison charts focus on.
    charted = pick_compared(stats, compare)
    print(f"Comparing in charts: {', '.join(charted)}")

    outdir = os.path.dirname(os.path.abspath(out_pdf)) or "."
    charts = make_charts(df, stats, charted, outdir)
    build_pdf(df, stats, charts, charted, out_pdf)

    # Clean up the temporary chart images.
    for p in charts.values():
        try:
            os.remove(p)
        except OSError:
            pass

    print(f"Done. Report written to: {out_pdf}")
    print(f"Networks in scorecard: {', '.join(stats.keys())}")


if __name__ == "__main__":
    main()