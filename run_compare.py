#!/usr/bin/env python3
"""
run_compare.py - Compare two CSV files and archive every output in one
timestamped folder.

This is the one command to run routinely. It performs the comparison, writes
the detailed CSV report, renders the PDF and PNG, and saves the console summary
as a text file — all inside a fresh folder named for the moment it ran, so
previous runs are never overwritten and the history is kept.

    results/
      20260816-142530/
        summary.txt        the full console comparison summary
        差分レポート.csv     every difference, one row per differing cell
        差分レポート.pdf     printable report
        差分レポート.png     shareable image

Usage:
    python3 run_compare.py A.CSV B.CSV
    python3 run_compare.py A.CSV B.CSV --label 7月分
    python3 run_compare.py A.CSV B.CSV --key 請求先コード,検針日
    python3 run_compare.py A.CSV B.CSV --ignore-kind width,whitespace

Exit codes:  0 = identical, 1 = differences found, 2 = error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import os
import re
import sys

import compare_csv
import export_report
from compare_csv import die

DEFAULT_BASENAME = "差分レポート"


def make_run_dir(base, label=""):
    """Create and return a fresh timestamped folder under `base`.

    If a folder for this second already exists (two runs in quick succession),
    a numeric suffix is added rather than overwriting the earlier run.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if label:
        # Keep the folder name filesystem-safe whatever the user passes in.
        safe = re.sub(r"[^\w.\-]+", "_", label, flags=re.UNICODE).strip("_")
        if safe:
            stamp = f"{stamp}_{safe}"

    path = os.path.join(base, stamp)
    if os.path.exists(path):
        n = 2
        while os.path.exists(f"{path}-{n}"):
            n += 1
        path = f"{path}-{n}"
    try:
        os.makedirs(path)
    except OSError as exc:
        die(f"cannot create output folder {path!r}: {exc}")
    return path


def run(func, argv, what):
    """Call a script's main(argv), converting its SystemExit into a code."""
    try:
        return func(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code == 2:
            raise SystemExit(2)
        return code
    except Exception as exc:                       # pragma: no cover - safety net
        die(f"{what} failed: {exc}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Compare two CSV files; archive CSV, PDF, PNG and summary "
                    "in one timestamped folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python3 run_compare.py A.CSV B.CSV
  python3 run_compare.py A.CSV B.CSV --label 7月分
  python3 run_compare.py A.CSV B.CSV --key 請求先コード,検針日
  python3 run_compare.py A.CSV B.CSV --ignore-kind width,whitespace
""")
    p.add_argument("file_a")
    p.add_argument("file_b")
    p.add_argument("--outdir", default="results",
                   help="parent folder for run folders (default: results)")
    p.add_argument("--label", default="",
                   help="optional text appended to the folder name")
    p.add_argument("--name", default=DEFAULT_BASENAME,
                   help=f"base name for the output files (default: {DEFAULT_BASENAME})")
    p.add_argument("--title", default="CSV差分レポート",
                   help="heading shown on the PDF/PNG")

    # Passed through to compare_csv.py
    p.add_argument("--key", default="")
    p.add_argument("--ignore-kind", default="")
    p.add_argument("--ignore-cols", default="")
    p.add_argument("--encoding")
    p.add_argument("--encoding-a")
    p.add_argument("--encoding-b")
    p.add_argument("--delimiter")

    # Passed through to export_report.py
    p.add_argument("--orientation", choices=["landscape", "portrait"],
                   default="landscape")
    p.add_argument("--max-rows", type=int, default=400)

    p.add_argument("--no-pdf", action="store_true", help="skip the PDF")
    p.add_argument("--no-png", action="store_true", help="skip the PNG")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print only the result and output paths; summary.txt is "
                        "still written in full")
    opts = p.parse_args(argv if argv is not None else sys.argv[1:])

    for f in (opts.file_a, opts.file_b):
        if not os.path.exists(f):
            die(f"input file not found: {f}")

    run_dir = make_run_dir(opts.outdir, opts.label)
    report = os.path.join(run_dir, opts.name + ".csv")

    # --- compare ---------------------------------------------------------
    cmp_argv = [opts.file_a, opts.file_b, "--report", report]
    for flag, value in (("--key", opts.key),
                        ("--ignore-kind", opts.ignore_kind),
                        ("--ignore-cols", opts.ignore_cols),
                        ("--encoding", opts.encoding),
                        ("--encoding-a", opts.encoding_a),
                        ("--encoding-b", opts.encoding_b),
                        ("--delimiter", opts.delimiter)):
        if value:
            cmp_argv += [flag, value]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run(compare_csv.main, cmp_argv, "comparison")
    summary = buf.getvalue()
    if opts.quiet:
        for line in summary.splitlines():
            if line.startswith("RESULT:"):
                print(line)
    else:
        print(summary, end="")

    summary_path = os.path.join(run_dir, "summary.txt")
    try:
        with open(summary_path, "w", encoding="utf-8") as fh:
            fh.write(f"A: {opts.file_a}\nB: {opts.file_b}\n"
                     f"run: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
            fh.write(summary)
    except OSError as exc:
        die(f"cannot write {summary_path!r}: {exc}")

    # --- render ----------------------------------------------------------
    if not (opts.no_pdf and opts.no_png):
        exp_argv = [report, "--title", opts.title,
                    "--subtitle", f"{os.path.basename(opts.file_a)}  ⇄  "
                                  f"{os.path.basename(opts.file_b)}",
                    "--orientation", opts.orientation,
                    "--max-rows", str(opts.max_rows)]
        if not opts.no_pdf:
            exp_argv += ["--pdf", os.path.join(run_dir, opts.name + ".pdf")]
        if not opts.no_png:
            exp_argv += ["--png", os.path.join(run_dir, opts.name + ".png")]
        # The per-file "written to" lines are redundant with the listing below.
        with contextlib.redirect_stdout(io.StringIO() if opts.quiet else sys.stdout):
            run(export_report.main, exp_argv, "rendering")

    print(f"\nAll output saved in: {run_dir}")
    for f in sorted(os.listdir(run_dir)):
        size = os.path.getsize(os.path.join(run_dir, f))
        print(f"  {f:<24} {size / 1024:>8.1f} KB")

    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
