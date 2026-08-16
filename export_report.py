#!/usr/bin/env python3
"""
export_report.py - Render a CSV difference report as PDF and PNG.

Takes the CSV written by `compare_csv.py --report` and produces a styled,
print-ready document. Differences are colour-coded by kind so that real data
changes stand out from cosmetic formatting noise.

Any other CSV is rendered as a plain table, so this doubles as a general
CSV-to-PDF/PNG tool.

Usage:
    python3 export_report.py 差分レポート.csv
    python3 export_report.py 差分レポート.csv --pdf out.pdf --png out.png
    python3 export_report.py data.csv --title "My table" --orientation portrait

With no --pdf/--png given, both are written next to the input file.

Requires Google Chrome / Chromium (used headlessly to render). Japanese text
renders correctly provided a CJK font such as Noto Sans CJK JP is installed.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import html
import os
import shutil
import subprocess
import sys
import tempfile

from compare_csv import KIND_HELP, detect_encoding, die

# Browsers tried in order for headless rendering.
BROWSERS = ("google-chrome", "chromium", "chromium-browser", "chrome",
            "google-chrome-stable", "/snap/bin/chromium")

# Difference kinds that are formatting-only rather than real data changes.
COSMETIC = {"whitespace", "width", "case", "number-format"}

# Columns hidden from the rendered table (too verbose or redundant on screen).
HIDDEN = {"kind_meaning", "column_no"}

# Per-kind accent colours, matched to the palette in the stylesheet.
KIND_COLOR = {
    "whitespace":    ("#7c6f5a", "#fdf6e3"),
    "width":         ("#7c6f5a", "#fdf6e3"),
    "case":          ("#7c6f5a", "#fdf6e3"),
    "number-format": ("#7c6f5a", "#fdf6e3"),
    "number":        ("#a4243b", "#fdeaed"),
    "missing":       ("#b5651d", "#fdf0e5"),
    "value":         ("#a4243b", "#fdeaed"),
    "row-only-in-A": ("#5b4b8a", "#f1eefa"),
    "row-only-in-B": ("#5b4b8a", "#f1eefa"),
}

STYLE = """
@page { size: %(page)s; margin: 11mm 10mm 13mm 10mm; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 22px 26px 26px;
  font-family: "Noto Sans CJK JP", "IPAexGothic", "Noto Sans", "Droid Sans Fallback", sans-serif;
  font-size: 11px; color: #23202b; background: #ffffff;
  -webkit-font-smoothing: antialiased;
}
h1 { margin: 0 0 3px; font-size: 19px; letter-spacing: .2px; }
.sub { color: #6d6880; font-size: 10.5px; margin-bottom: 15px; }
.sub code { background: #f2f1f6; padding: 1px 5px; border-radius: 3px; font-size: 10px; }

.cards { display: flex; gap: 9px; flex-wrap: wrap; margin-bottom: 15px; }
.card {
  border: 1px solid #e2e0ea; border-radius: 7px; padding: 8px 13px;
  min-width: 96px; background: #fbfbfd;
}
.card .n { font-size: 19px; font-weight: 700; line-height: 1.15; }
.card .l { font-size: 9.5px; color: #6d6880; text-transform: uppercase; letter-spacing: .5px; }
.card.real .n { color: #a4243b; }
.card.cos  .n { color: #7c6f5a; }

.legend { margin-bottom: 13px; font-size: 10px; color: #55506a; }
.pill {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  margin: 0 5px 5px 0; border: 1px solid transparent; font-size: 9.5px;
}

table { border-collapse: collapse; width: 100%%; }
thead th {
  background: #f2f1f6; text-align: left; font-size: 9.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .45px; color: #55506a;
  padding: 7px 8px; border-bottom: 1.5px solid #d9d6e3; white-space: nowrap;
}
tbody td {
  padding: 6px 8px; border-bottom: 1px solid #eeecf3;
  vertical-align: top; word-break: break-word;
}
tbody tr:nth-child(even) { background: #fafaFC; }
tr { page-break-inside: avoid; }
thead { display: table-header-group; }

td.num { text-align: right; color: #6d6880; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.col { font-weight: 600; white-space: nowrap; }
td.va, td.vb { font-family: "Noto Sans Mono CJK JP", ui-monospace, monospace; font-size: 10px; }
td.va { color: #1d4e6f; }
td.vb { color: #8a4a12; }
.blank { color: #b6b2c4; font-style: italic; font-family: sans-serif; }
.kind { white-space: nowrap; font-size: 9.5px; padding: 1.5px 7px; border-radius: 9px; display: inline-block; }

.foot { margin-top: 14px; font-size: 9.5px; color: #8b8799; }
.trunc {
  margin-top: 10px; padding: 7px 11px; border-radius: 6px;
  background: #fdf6e3; border: 1px solid #ecdfba; color: #7a6a3f; font-size: 10px;
}
.empty { padding: 26px; text-align: center; color: #6d6880;
         border: 1px dashed #d9d6e3; border-radius: 8px; background: #fbfbfd; }
"""


def find_browser(explicit=None):
    for name in ((explicit,) if explicit else BROWSERS):
        path = shutil.which(name) or (name if os.path.exists(name) else None)
        if path:
            return path
    die("no Chrome/Chromium found for rendering.\n"
        "       Install Google Chrome or Chromium, or pass --browser PATH.")


def read_csv(path, encoding=None):
    try:
        enc = detect_encoding(path, encoding)
        with open(path, encoding=enc, newline="") as fh:
            rows = [r for r in csv.reader(fh) if r]
    except OSError as exc:
        die(f"cannot read {path!r}: {exc}")
    except csv.Error as exc:
        die(f"malformed CSV in {path!r}: {exc}")
    if not rows:
        die(f"{path!r} contains no rows.")
    return rows[0], rows[1:]


def is_diff_report(header):
    return {"value_in_A", "value_in_B", "diff_kind"} <= set(header)


def cell(value, klass=""):
    if value == "":
        return f'<td class="{klass}"><span class="blank">(blank)</span></td>'
    return f'<td class="{klass}">{html.escape(value)}</td>'


def build_html(header, rows, opts, source, truncated, all_rows=None):
    """Render the document. `rows` is what gets tabulated; `all_rows` (the full,
    untruncated set) is what the summary counts are computed from, so the totals
    stay honest when --max-rows clips the table."""
    diff_mode = is_diff_report(header)
    idx = {name: i for i, name in enumerate(header)}
    all_rows = rows if all_rows is None else all_rows

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else ""

    # Drop verbose columns, and any column that is empty in every row (e.g. the
    # `key` column when the comparison ran positionally).
    visible = [c for c in header if c not in HIDDEN]
    visible = [c for c in visible
               if any(get(r, c) for r in rows) or c in ("value_in_A", "value_in_B")]

    parts = ['<meta charset="utf-8">',
             f"<style>{STYLE % {'page': 'A4 ' + opts.orientation}}</style>",
             f"<h1>{html.escape(opts.title)}</h1>"]

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts.append(f'<div class="sub">Source: <code>{html.escape(os.path.basename(source))}'
                 f"</code> &nbsp;·&nbsp; generated {stamp}</div>")

    if diff_mode:
        kinds = {}
        for r in all_rows:
            k = get(r, "diff_kind")
            kinds[k] = kinds.get(k, 0) + 1
        total = len(all_rows)
        cos = sum(n for k, n in kinds.items() if k in COSMETIC)
        real = total - cos

        parts.append('<div class="cards">')
        parts.append(f'<div class="card"><div class="n">{total}</div>'
                     f'<div class="l">Differences</div></div>')
        parts.append(f'<div class="card real"><div class="n">{real}</div>'
                     f'<div class="l">Substantive</div></div>')
        parts.append(f'<div class="card cos"><div class="n">{cos}</div>'
                     f'<div class="l">Cosmetic</div></div>')
        for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
            parts.append(f'<div class="card"><div class="n">{n}</div>'
                         f'<div class="l">{html.escape(k)}</div></div>')
        parts.append("</div>")

        parts.append('<div class="legend">')
        for k in sorted(kinds):
            fg, bg = KIND_COLOR.get(k, ("#55506a", "#f2f1f6"))
            meaning = KIND_HELP.get(k, "row present in only one file")
            parts.append(f'<span class="pill" style="color:{fg};background:{bg};'
                         f'border-color:{fg}33">{html.escape(k)}</span>'
                         f"{html.escape(meaning)}<br>")
        parts.append("</div>")

    if not rows:
        parts.append('<div class="empty">No differences — the two files are identical.</div>')
    else:
        parts.append("<table><thead><tr>")
        for c in visible:
            parts.append(f"<th>{html.escape(c)}</th>")
        parts.append("</tr></thead><tbody>")

        for r in rows:
            parts.append("<tr>")
            for c in visible:
                v = get(r, c)
                if c == "diff_kind":
                    fg, bg = KIND_COLOR.get(v, ("#55506a", "#f2f1f6"))
                    parts.append(f'<td><span class="kind" style="color:{fg};'
                                 f'background:{bg}">{html.escape(v)}</span></td>')
                elif c == "value_in_A":
                    parts.append(cell(v, "va"))
                elif c == "value_in_B":
                    parts.append(cell(v, "vb"))
                elif c in ("row", "line_in_A", "line_in_B", "column_no"):
                    parts.append(cell(v, "num"))
                elif c == "column_name":
                    parts.append(cell(v, "col"))
                else:
                    parts.append(cell(v))
            parts.append("</tr>")
        parts.append("</tbody></table>")

    if truncated:
        parts.append(f'<div class="trunc">Showing the first {len(rows)} of '
                     f"{len(rows) + truncated} rows. Raise --max-rows to include more.</div>")
    parts.append('<div class="foot">Generated by compare_csv.py / export_report.py</div>')
    return "\n".join(parts)


def run_browser(browser, html_path, extra, label):
    cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
           "--disable-dev-shm-usage", "--hide-scrollbars",
           "--run-all-compositor-stages-before-draw",
           "--virtual-time-budget=6000"] + extra + [f"file://{html_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        die(f"{label} rendering failed (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout).strip()[:600]}")


def autocrop_bottom(path, pad=16):
    """Trim the dead whitespace below the content of a screenshot."""
    try:
        from PIL import Image
    except ImportError:
        return
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        bg = im.getpixel((w - 2, h - 2))
        px = im.load()
        last = 0
        for y in range(h - 1, -1, -1):
            row_has_content = False
            for x in range(0, w, 3):          # sample every 3rd pixel: fast, ample
                if px[x, y] != bg:
                    row_has_content = True
                    break
            if row_has_content:
                last = y
                break
        if last and last < h - pad:
            im.crop((0, 0, w, min(h, last + pad))).save(path)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Render a CSV difference report as PDF and PNG.")
    p.add_argument("csv_file", help="CSV to render (e.g. the --report output)")
    p.add_argument("--pdf", metavar="PATH", help="PDF output path")
    p.add_argument("--png", metavar="PATH", help="PNG output path")
    p.add_argument("--title", default="CSV Difference Report")
    p.add_argument("--orientation", choices=["landscape", "portrait"],
                   default="landscape", help="PDF page orientation")
    p.add_argument("--max-rows", type=int, default=400,
                   help="maximum table rows to render (default: 400)")
    p.add_argument("--width", type=int, default=1500,
                   help="PNG render width in CSS px (default: 1500)")
    p.add_argument("--scale", type=float, default=2.0,
                   help="PNG device scale factor (default: 2.0)")
    p.add_argument("--encoding", help="force input encoding (default: auto-detect)")
    p.add_argument("--browser", help="path to Chrome/Chromium")
    p.add_argument("--keep-html", metavar="PATH", help="also save the intermediate HTML")
    opts = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not opts.pdf and not opts.png:
        stem = os.path.splitext(opts.csv_file)[0]
        opts.pdf, opts.png = stem + ".pdf", stem + ".png"

    browser = find_browser(opts.browser)
    header, rows = read_csv(opts.csv_file, opts.encoding)

    all_rows = rows
    truncated = max(0, len(rows) - opts.max_rows)
    rows = rows[:opts.max_rows]

    doc = build_html(header, rows, opts, opts.csv_file, truncated, all_rows)
    if opts.keep_html:
        with open(opts.keep_html, "w", encoding="utf-8") as fh:
            fh.write(doc)

    tmp_dir = tempfile.mkdtemp(prefix="csvreport_")
    html_path = os.path.join(tmp_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(doc)

    try:
        if opts.pdf:
            run_browser(browser, html_path,
                        [f"--print-to-pdf={os.path.abspath(opts.pdf)}",
                         "--no-pdf-header-footer"], "PDF")
            print(f"PDF written to: {opts.pdf}")

        if opts.png:
            # Estimate a window tall enough for the whole table, then trim the
            # unused space afterwards — headless Chrome has no full-page flag.
            height = 420 + 34 * len(rows) + (150 if is_diff_report(header) else 0)
            run_browser(browser, html_path,
                        [f"--screenshot={os.path.abspath(opts.png)}",
                         f"--window-size={opts.width},{min(height, 30000)}",
                         f"--force-device-scale-factor={opts.scale}"], "PNG")
            autocrop_bottom(opts.png)
            print(f"PNG written to: {opts.png}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except subprocess.TimeoutExpired:
        die("rendering timed out.")
