#!/usr/bin/env python3
"""Build the one-page poster summary (one table, two charts) from a results directory.

    python3 make_poster_page.py <results-dir> <out.html> [--pdf out.pdf]
        [--timing-repeats DIR [DIR ...]] [--timing-note TEXT]

The page is a single self-contained HTML file (inline SVG, no scripts). `--pdf`
prints it to one A4 page with headless Chromium, which must be on the PATH as
`chromium` or `chromium-browser`. `--timing-repeats` takes the results
directories of repeated planner-only runs of some of the same scenarios (for
example the 1 to 3 train cells). For the train counts they cover, the planner's
time in the time chart is then the median, over the scenarios that every
repetition solved, of each scenario's median time over the repetitions; other
train counts keep the single sweep's median. `--timing-note` adds a free-text
note under the time chart. Only reads the results directories.
"""

import argparse
import collections
import math
import os
import shutil
import statistics
import subprocess
import sys

import results_lib as rl

PLANNER_COLOUR, SOLVER_COLOUR = "#2b6cb0", "#c05621"
CAUSES = [("timeout", "search timeout", PLANNER_COLOUR),
          ("late/early departure", "late/early departure", SOLVER_COLOUR),
          ("track conflict", "track conflict", "#6b8e23"),
          (None, "other", "#888")]

CSS = """
body{font-family:system-ui,sans-serif;max-width:920px;margin:0 auto;padding:16px;color:#222;background:#fff;line-height:1.45}
h1{font-size:1.4rem} h2{font-size:1.1rem;margin:22px 0 4px} p{margin:4px 0} .msg{font-style:italic;color:#444}
.setup{font-size:.85rem;background:#f4f6f8;border:1px solid #dde;padding:8px 12px;border-radius:6px}
svg{width:100%;height:auto;background:#fafafa;border:1px solid #eee} .one{max-width:460px}
.legend{display:flex;gap:14px;font-size:.85rem;margin:4px 0} .legend i{display:inline-block;width:12px;height:12px;margin-right:5px;vertical-align:-1px}
.y{font-size:.8rem;color:#666;margin:2px 0} table{border-collapse:collapse;font-size:.85rem;margin-top:6px}
th,td{border:1px solid #ccc;padding:3px 7px;text-align:right} th{text-align:center} td small{color:#777} tr.tot td{font-weight:600;background:#f4f6f8}
@page{size:A4;margin:9mm} @media print{body{font-size:11.5px;padding:0;max-width:none}h1{font-size:1.15rem;margin:6px 0}h2{font-size:1rem;margin:10px 0 2px}.one{max-width:330px}}
"""


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def repeated_planner_medians(repeat_dirs):
    """train count -> median over scenarios of the scenario's median planner
    time across the repetitions, for scenarios every repetition solved."""
    per_rep = [{r["instance"]: r for r in rl.runs(d) if r["tool"] == "planner"} for d in repeat_dirs]
    medians = collections.defaultdict(list)
    for instance in sorted(set.intersection(*(set(p) for p in per_rep))):
        runs = [p[instance] for p in per_rep]
        if all(r["accepted"] for r in runs):
            medians[runs[0]["trains"]].append(statistics.median(r["seconds"] for r in runs))
    return {n: statistics.median(v) for n, v in medians.items()}


def collect(results):
    rows = rl.runs(results)
    cells = collections.defaultdict(lambda: [0, 0])
    secs = collections.defaultdict(list)
    for r in rows:
        cells[(r["tool"], r["trains"], r["matching"])][0] += 1
        if r["accepted"]:
            cells[(r["tool"], r["trains"], r["matching"])][1] += 1
            secs[(r["tool"], r["trains"])].append(r["seconds"])
    fails = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["tool"] == "planner" and not r["accepted"]:
            cause = rl.failure_cause(results, r["instance"], "planner")
            known = {c for c, _, _ in CAUSES if c}
            fails[r["trains"]][cause if cause in known else None] += 1
    return cells, secs, fails


def table_html(cells, trains, matchings):
    head = ("<tr><th rowspan=2>trains</th>" + "".join(f"<th colspan=2>{m}</th>" for m in matchings)
            + "</tr><tr>" + "<th>planner</th><th>solver</th>" * len(matchings) + "</tr>")
    body = ""
    for n in trains:
        body += f"<tr><td>{n}</td>"
        for m in matchings:
            for t in rl.TOOLS:
                n_all, ok = cells[(t, n, m)]
                body += f"<td>{pct(ok, n_all)} <small>({ok}/{n_all})</small></td>"
        body += "</tr>"
    body += "<tr class=tot><td>all</td>"
    for m in matchings:
        for t in rl.TOOLS:
            ok = sum(cells[(t, n, m)][1] for n in trains)
            n_all = sum(cells[(t, n, m)][0] for n in trains)
            body += f"<td>{pct(ok, n_all)} <small>({ok}/{n_all})</small></td>"
    return f"<table>{head}{body}</tr></table>"


def _axes(width, left, bottom, top, ticks, ymap):
    out = []
    for value, label in ticks:
        y = ymap(value)
        out.append(f'<line x1="{left}" x2="{width - 10}" y1="{y:.1f}" y2="{y:.1f}" stroke="#ddd"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" font-size="10" text-anchor="end" fill="#555">{label}</text>')
    return out


def time_chart(secs, trains, planner_override=None):
    width, height, left, bottom, top = 420, 240, 40, 36, 16
    plot_w, plot_h = width - left - 10, height - bottom - top
    top_log = math.log10(200)
    ymap = lambda v: top + plot_h - plot_h * math.log10(max(v, 1)) / top_log
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="median time to a plan">']
    out += _axes(width, left, bottom, top, [(1, "1"), (10, "10"), (100, "100")], ymap)
    group_w = plot_w / len(trains)
    bar_w = group_w * 0.35
    for i, n in enumerate(trains):
        x0 = left + i * group_w + group_w * 0.15
        for j, (colour, tool) in enumerate(((PLANNER_COLOUR, "planner"), (SOLVER_COLOUR, "solver"))):
            if tool == "planner" and n in (planner_override or {}):
                value = planner_override[n]
            elif secs[(tool, n)]:
                value = statistics.median(secs[(tool, n)])
            else:
                continue
            y = ymap(value)
            out.append(f'<rect x="{x0 + j * bar_w:.1f}" y="{y:.1f}" width="{bar_w - 1:.1f}" height="{top + plot_h - y:.1f}" fill="{colour}"/>'
                       f'<text x="{x0 + j * bar_w + bar_w / 2 - .5:.1f}" y="{y - 3:.1f}" font-size="9" text-anchor="middle" fill="#333">{value:.0f}</text>')
        out.append(f'<text x="{left + i * group_w + group_w / 2:.1f}" y="{height - bottom + 16}" font-size="11" text-anchor="middle">{n}</text>')
    out.append(f'<text x="{left + plot_w / 2}" y="{height - 4}" font-size="11" text-anchor="middle">number of trains</text></svg>')
    return "".join(out)


def failure_chart(fails, trains, per_train):
    width, height, left, bottom, top = 420, 240, 40, 36, 16
    plot_w, plot_h = width - left - 10, height - bottom - top
    worst = max(sum(fails[n].values()) for n in trains) * 100 / per_train
    ymax = max(25, math.ceil(worst / 10) * 10)
    ymap = lambda v: top + plot_h - plot_h * v / ymax
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="planner failure causes">']
    out += _axes(width, left, bottom, top, [(v, str(v)) for v in range(0, ymax + 1, ymax // 4)], ymap)
    group_w = plot_w / len(trains)
    bar_w = group_w * 0.55
    for i, n in enumerate(trains):
        x, stacked = left + i * group_w + (group_w - bar_w) / 2, 0
        for key, _, colour in CAUSES:
            value = 100 * fails[n][key] / per_train
            if value <= 0:
                continue
            y_top, y_bottom = ymap(stacked + value), ymap(stacked)
            stacked += value
            out.append(f'<rect x="{x:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{y_bottom - y_top:.1f}" fill="{colour}"/>')
        out.append(f'<text x="{left + i * group_w + group_w / 2:.1f}" y="{height - bottom + 16}" font-size="11" text-anchor="middle">{n}</text>')
    out.append(f'<text x="{left + plot_w / 2}" y="{height - 4}" font-size="11" text-anchor="middle">number of trains</text></svg>')
    return "".join(out)


def legend(items):
    return '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{l}</span>' for l, c in items) + "</div>"


def build(results, timing_note="", repeat_dirs=()):
    exp = rl.load_experiment(results)
    cells, secs, fails = collect(results)
    trains = sorted({k[1] for k in cells})
    matchings = [m for m in rl.MATCHINGS if any(k[2] == m for k in cells)]
    per_cell = max(v[0] for v in cells.values())
    total = per_cell * len(trains) * len(matchings)
    ok = {t: sum(c[1] for k, c in cells.items() if k[0] == t) for t in rl.TOOLS}
    versions = exp.get("versions", {})
    override = repeated_planner_medians(repeat_dirs) if repeat_dirs else {}
    medians = {t: [override[n] if t == "planner" and n in override else statistics.median(secs[(t, n)])
                   for n in trains if secs[(t, n)] or (t == "planner" and n in override)] for t in rl.TOOLS}
    span = lambda v: f"{min(v):.0f}&ndash;{max(v):.0f}"
    parts = []
    if override:
        lo, hi = min(override), max(override)
        parts.append(f"Planner times for {lo}&ndash;{hi} trains are the median over {len(repeat_dirs)} repetition{"s" if len(repeat_dirs) != 1 else ""} of those scenarios; "
                     f"other train counts come from the single sweep.")
    if timing_note:
        parts.append(timing_note)
    note = (" " + " ".join(parts)) if parts else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Planner evaluation: poster panels</title><style>{CSS}</style></head><body>
<h1>Planner vs solver on generated Kleine Binckhorst scenarios</h1>
<p class="setup">{per_cell} scenarios per cell, {len(trains)} train counts &times; {len(matchings)} matchings = {total} scenarios; planner ({exp.get("planner")}, image <code>{versions.get("planner")}</code>) and solver run on identical instances; generator, solver and evaluator <code>{versions.get("generator")}</code>/<code>{versions.get("solver")}</code>/<code>{versions.get("evaluator")}</code>; no servicing; <b>{exp.get("max_duration")} s per scenario for both tools</b>. A scenario counts as solved only if the TORS evaluator accepts the plan.</p>
<h2>1. How many scenarios are solved?</h2><p class="msg">Overall the two tools are level ({pct(ok["planner"], total)} planner, {pct(ok["solver"], total)} solver). The planner is clearly ahead on random matching; the solver is ahead on FIFO and LIFO.</p>{table_html(cells, trains, matchings)}
<h2>2. Time to a plan</h2><p class="msg">The solver needs about {span(medians["solver"])} s; the planner needs about {span(medians["planner"])} s.</p><div class="one">{time_chart(secs, trains, override)}</div>{legend([("planner", PLANNER_COLOUR), ("solver", SOLVER_COLOUR)])}
<p class="y">y-axis: median seconds to an accepted plan (log scale).{note}</p>
<h2>3. Why the planner fails</h2><p class="msg">Failures start at 2&ndash;3 trains and are dominated by late departures and search timeouts.</p><div class="one">{failure_chart(fails, trains, per_cell * len(matchings))}</div>{legend([(l, c) for _, l, c in CAUSES])}
<p class="y">y-axis: % of the planner's scenarios at that train count ({per_cell * len(matchings)} each) that fail, by cause.</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("out_html")
    ap.add_argument("--pdf")
    ap.add_argument("--timing-repeats", nargs="+", default=[], metavar="DIR")
    ap.add_argument("--timing-note", default="")
    args = ap.parse_args()
    with open(args.out_html, "w") as f:
        f.write(build(args.results, args.timing_note, args.timing_repeats))
    if args.pdf:
        browser = shutil.which("chromium") or shutil.which("chromium-browser")
        if not browser:
            sys.exit("--pdf needs chromium or chromium-browser on the PATH")
        subprocess.run([browser, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                        f"--print-to-pdf={os.path.abspath(args.pdf)}", f"file://{os.path.abspath(args.out_html)}"],
                       check=True, capture_output=True, timeout=120)


if __name__ == "__main__":
    main()
