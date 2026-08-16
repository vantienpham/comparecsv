#!/usr/bin/env python3
"""
compare_csv.py - Find every difference between two CSV files.

Answers three questions:
  1. Do the two files have the same number of columns (and the same headers)?
  2. Do they have the same number of rows?
  3. Is every single cell identical? If not, exactly which ones differ?

Designed for large files: rows are streamed and compared one pair at a time in
positional mode, so memory use stays flat regardless of file size.

Each difference is classified so that cosmetic noise (quoting, fullwidth vs
halfwidth characters, padding, number formatting) is visually separated from
real changes in the data.

Usage:
    python3 compare_csv.py FILE_A FILE_B
    python3 compare_csv.py FILE_A FILE_B --report diff.csv
    python3 compare_csv.py FILE_A FILE_B --key 請求先コード,検針日
    python3 compare_csv.py FILE_A FILE_B --ignore-kind width,case,whitespace

Exit codes:  0 = identical, 1 = differences found, 2 = error.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import sys
import unicodedata
from collections import Counter, OrderedDict

# Encodings tried in order when none is given. utf-8 is tried before the
# Japanese codecs because cp932 will happily decode almost any byte sequence
# into mojibake rather than raising, so it must be the fallback, never first.
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp932", "euc_jp", "utf-16")

# Whitespace stripped by the whitespace classifier / --trim. Includes U+3000
# IDEOGRAPHIC SPACE, which is extremely common in Japanese business exports.
WHITESPACE = " \t\r\n　\xa0"

# Difference kinds, ordered from most cosmetic to most significant.
KINDS = ("whitespace", "width", "case", "number-format", "number", "missing", "value")

KIND_HELP = {
    "whitespace": "same text, different leading/trailing spaces",
    "width": "same text, fullwidth vs halfwidth (or other NFKC) form",
    "case": "same text, different upper/lower case",
    "number-format": "same number, written differently (1,000 vs 1000)",
    "number": "different numeric value",
    "missing": "value present in one file, blank in the other",
    "value": "different text",
}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def die(message):
    """Report a usage/IO problem and exit with the documented error code."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def decodes_fully(path, enc, chunk_size=1 << 20):
    """True if `enc` decodes the entire file cleanly.

    The file is fed through an incremental decoder in chunks so that memory
    stays flat on multi-gigabyte inputs, and so a multi-byte character split
    across a chunk boundary is not mistaken for a decode error.
    """
    try:
        decoder = codecs.getincrementaldecoder(enc)(errors="strict")
    except LookupError:
        return False
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            try:
                decoder.decode(chunk, final=not chunk)
            except UnicodeDecodeError:
                return False
            if not chunk:
                return True


def detect_encoding(path, forced=None):
    """Return an encoding that decodes the whole file without error."""
    candidates = (forced,) if forced else ENCODING_CANDIDATES
    for enc in candidates:
        if decodes_fully(path, enc):
            return enc
    die(f"could not decode {path!r}. Tried: {', '.join(candidates)}.\n"
        f"       Pass --encoding to specify it explicitly.")


def detect_delimiter(path, encoding):
    """Sniff the delimiter, defaulting to comma when ambiguous."""
    with open(path, encoding=encoding, newline="") as fh:
        sample = fh.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


class CsvSource:
    """A CSV file opened for streaming row-by-row reads."""

    def __init__(self, path, encoding=None, delimiter=None):
        self.path = path
        self.encoding = detect_encoding(path, encoding)
        self.delimiter = delimiter or detect_delimiter(path, self.encoding)
        self._fh = open(path, encoding=self.encoding, newline="")
        self._reader = csv.reader(self._fh, delimiter=self.delimiter)
        try:
            self.header = next(self._reader)
        except StopIteration:
            self.header = []

    def rows(self):
        """Yield (line_number, row). Blank trailing lines are skipped."""
        for row in self._reader:
            if len(row) == 1 and row[0] == "":
                continue
            yield self._reader.line_num, row

    def close(self):
        self._fh.close()


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def strip_ws(s):
    return s.strip(WHITESPACE)


def parse_number(s):
    """Return float for numeric-looking text, else None. Handles 1,000 and ¥/% wrappers."""
    t = strip_ws(s).replace(",", "").replace("，", "")
    t = t.lstrip("¥$€£").rstrip("%")
    t = unicodedata.normalize("NFKC", t)
    if not t or t in "+-":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def classify(a, b):
    """Classify how two unequal cell values differ. Returns a kind from KINDS."""
    sa, sb = strip_ws(a), strip_ws(b)
    if sa == sb:
        return "whitespace"

    na, nb = unicodedata.normalize("NFKC", sa), unicodedata.normalize("NFKC", sb)
    if na == nb:
        return "width"
    if na.casefold() == nb.casefold():
        return "case"

    va, vb = parse_number(sa), parse_number(sb)
    if va is not None and vb is not None:
        return "number-format" if va == vb else "number"

    if not sa or not sb:
        return "missing"
    return "value"


class Diff:
    __slots__ = ("row_no", "line_a", "line_b", "key", "col_index", "col_name",
                 "value_a", "value_b", "kind")

    def __init__(self, row_no, line_a, line_b, key, col_index, col_name,
                 value_a, value_b, kind):
        self.row_no = row_no
        self.line_a = line_a
        self.line_b = line_b
        self.key = key
        self.col_index = col_index
        self.col_name = col_name
        self.value_a = value_a
        self.value_b = value_b
        self.kind = kind


class Result:
    def __init__(self):
        self.header_a = []
        self.header_b = []
        self.header_match = True
        self.header_notes = []
        self.rows_a = 0
        self.rows_b = 0
        self.ragged = []          # (side, line_no, width, expected)
        self.diffs = []           # list[Diff]
        self.diffs_truncated = 0
        self.only_in_a = []       # (key, line_no)
        self.only_in_b = []
        self.rows_compared = 0
        self.rows_with_diff = 0
        self.kind_counts = Counter()
        self.column_counts = Counter()

    @property
    def identical(self):
        return (self.header_match and not self.ragged and not self.diffs
                and not self.diffs_truncated and not self.only_in_a
                and not self.only_in_b and self.rows_a == self.rows_b)


def compare_headers(res, ha, hb):
    """Record structural differences between the two header rows."""
    res.header_a, res.header_b = ha, hb
    if len(ha) != len(hb):
        res.header_match = False
        res.header_notes.append(
            f"column count differs: file A has {len(ha)}, file B has {len(hb)}")
    if [strip_ws(c) for c in ha] != [strip_ws(c) for c in hb]:
        res.header_match = False
        for i in range(max(len(ha), len(hb))):
            ca = ha[i] if i < len(ha) else "<missing>"
            cb = hb[i] if i < len(hb) else "<missing>"
            if strip_ws(ca) != strip_ws(cb):
                res.header_notes.append(
                    f"column {i + 1}: A={ca!r} B={cb!r}")


def compare_row(res, opts, row_no, line_a, line_b, key, ra, rb, ncols, names):
    """Compare one aligned pair of rows, appending any diffs to res."""
    found = False
    # A ragged row may carry fields beyond the header width; widen the scan so
    # those trailing values are still compared rather than silently dropped.
    ncols = max(ncols, len(ra), len(rb))
    for j in range(ncols):
        if j in opts.ignore_cols:
            continue
        va = ra[j] if j < len(ra) else ""
        vb = rb[j] if j < len(rb) else ""
        if va == vb:
            continue

        kind = classify(va, vb)
        if kind in opts.ignore_kinds:
            continue

        found = True
        res.kind_counts[kind] += 1
        name = names[j] if j < len(names) else f"col{j + 1}"
        res.column_counts[name] += 1
        if len(res.diffs) < opts.max_diffs:
            res.diffs.append(
                Diff(row_no, line_a, line_b, key, j + 1, name, va, vb, kind))
        else:
            res.diffs_truncated += 1
    if found:
        res.rows_with_diff += 1


def check_width(res, side, line_no, row, expected):
    if len(row) != expected:
        res.ragged.append((side, line_no, len(row), expected))


def compare_positional(res, opts, src_a, src_b):
    """Stream both files in parallel, comparing row N of A against row N of B."""
    ncols = max(len(src_a.header), len(src_b.header))
    names = src_a.header if len(src_a.header) >= len(src_b.header) else src_b.header

    ia, ib = src_a.rows(), src_b.rows()
    row_no = 0
    while True:
        a = next(ia, None)
        b = next(ib, None)
        if a is None and b is None:
            break
        row_no += 1

        if a is not None:
            res.rows_a += 1
            check_width(res, "A", a[0], a[1], len(src_a.header))
        if b is not None:
            res.rows_b += 1
            check_width(res, "B", b[0], b[1], len(src_b.header))

        if a is None:
            res.only_in_b.append((f"row {row_no}", b[0]))
            continue
        if b is None:
            res.only_in_a.append((f"row {row_no}", a[0]))
            continue

        res.rows_compared += 1
        compare_row(res, opts, row_no, a[0], b[0], "", a[1], b[1], ncols, names)

    # Drain any remainder (only one iterator can still have rows).
    for line_no, row in ia:
        row_no += 1
        res.rows_a += 1
        check_width(res, "A", line_no, row, len(src_a.header))
        res.only_in_a.append((f"row {row_no}", line_no))
    for line_no, row in ib:
        row_no += 1
        res.rows_b += 1
        check_width(res, "B", line_no, row, len(src_b.header))
        res.only_in_b.append((f"row {row_no}", line_no))


def key_indices(header, key_names):
    """Resolve --key names (or 1-based numbers) to column indices."""
    stripped = [strip_ws(c) for c in header]
    out = []
    for name in key_names:
        name = strip_ws(name)
        if name in stripped:
            out.append(stripped.index(name))
        elif name.isdigit() and 1 <= int(name) <= len(header):
            out.append(int(name) - 1)
        else:
            die(f"column {name!r} not found in header.\n"
                f"       Available: {', '.join(stripped)}")
    return out


def compare_by_key(res, opts, src_a, src_b):
    """Match rows by key column(s) rather than position.

    File B is indexed in memory; file A is streamed against it. Use this when
    rows may be reordered, inserted or deleted between the two files.
    """
    ncols = max(len(src_a.header), len(src_b.header))
    names = src_a.header if len(src_a.header) >= len(src_b.header) else src_b.header
    ka = key_indices(src_a.header, opts.key)
    kb = key_indices(src_b.header, opts.key)

    def make_key(row, idx):
        return "|".join(strip_ws(row[i]) if i < len(row) else "" for i in idx)

    # Index B. Duplicate keys are kept in arrival order and consumed pairwise.
    index = OrderedDict()
    for line_no, row in src_b.rows():
        res.rows_b += 1
        check_width(res, "B", line_no, row, len(src_b.header))
        index.setdefault(make_key(row, kb), []).append((line_no, row))

    row_no = 0
    for line_no, row in src_a.rows():
        res.rows_a += 1
        row_no += 1
        check_width(res, "A", line_no, row, len(src_a.header))
        key = make_key(row, ka)
        bucket = index.get(key)
        if not bucket:
            res.only_in_a.append((key, line_no))
            continue
        b_line, b_row = bucket.pop(0)
        if not bucket:
            del index[key]
        res.rows_compared += 1
        compare_row(res, opts, row_no, line_no, b_line, key, row, b_row, ncols, names)

    for key, bucket in index.items():
        for b_line, _ in bucket:
            res.only_in_b.append((key, b_line))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def write_report(path, res, opts, encoding):
    with open(path, "w", encoding=encoding, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["row", "key", "line_in_A", "line_in_B", "column_no",
                    "column_name", "value_in_A", "value_in_B", "diff_kind",
                    "kind_meaning"])
        for d in res.diffs:
            w.writerow([d.row_no, d.key, d.line_a, d.line_b, d.col_index,
                        d.col_name, d.value_a, d.value_b, d.kind,
                        KIND_HELP[d.kind]])
        for key, line in res.only_in_a:
            w.writerow([key, key, line, "", "", "<entire row>", "<present>",
                        "<missing>", "row-only-in-A", "row exists only in file A"])
        for key, line in res.only_in_b:
            w.writerow([key, key, "", line, "", "<entire row>", "<missing>",
                        "<present>", "row-only-in-B", "row exists only in file B"])


def bar(n, total, width=28):
    if not total:
        return ""
    filled = max(1, round(width * n / total)) if n else 0
    return "#" * filled


def print_summary(res, opts, src_a, src_b, name_a, name_b):
    out = sys.stdout
    line = "=" * 68

    print(line)
    print("CSV COMPARISON")
    print(line)
    print(f"  A: {name_a}")
    print(f"     encoding={src_a.encoding}  delimiter={src_a.delimiter!r}  "
          f"columns={len(src_a.header)}  data rows={res.rows_a}")
    print(f"  B: {name_b}")
    print(f"     encoding={src_b.encoding}  delimiter={src_b.delimiter!r}  "
          f"columns={len(src_b.header)}  data rows={res.rows_b}")
    print()

    # --- structure -------------------------------------------------------
    print("STRUCTURE")
    if len(src_a.header) == len(src_b.header):
        print(f"  [OK]   column count identical ({len(src_a.header)} columns)")
    else:
        print(f"  [DIFF] column count differs: "
              f"A={len(src_a.header)}  B={len(src_b.header)}")
    if res.header_match:
        print("  [OK]   header names identical")
    else:
        print("  [DIFF] header names differ:")
        for note in res.header_notes:
            print(f"           {note}")
    if res.rows_a == res.rows_b:
        print(f"  [OK]   row count identical ({res.rows_a} data rows)")
    else:
        print(f"  [DIFF] row count differs: A={res.rows_a}  B={res.rows_b}")
    if res.ragged:
        print(f"  [DIFF] {len(res.ragged)} row(s) have an unexpected column count:")
        for side, line_no, got, exp in res.ragged[:10]:
            print(f"           file {side} line {line_no}: {got} fields, expected {exp}")
        if len(res.ragged) > 10:
            print(f"           ... and {len(res.ragged) - 10} more")
    else:
        print("  [OK]   every row has the expected number of fields")
    print()

    # --- unmatched rows --------------------------------------------------
    if res.only_in_a or res.only_in_b:
        print("UNMATCHED ROWS")
        print(f"  only in A: {len(res.only_in_a)}      only in B: {len(res.only_in_b)}")
        for key, ln in res.only_in_a[:5]:
            print(f"    A-only  line {ln}  {key}")
        for key, ln in res.only_in_b[:5]:
            print(f"    B-only  line {ln}  {key}")
        extra = len(res.only_in_a) + len(res.only_in_b)
        if extra > 10:
            print(f"    ... and {extra - 10} more (see report)")
        print()

    # --- cells -----------------------------------------------------------
    total = sum(res.kind_counts.values())
    print("CELL VALUES")
    if total == 0:
        print(f"  [OK]   all cells identical across {res.rows_compared} compared rows")
    else:
        print(f"  [DIFF] {total} differing cell(s) in {res.rows_with_diff} "
              f"of {res.rows_compared} compared rows")
        print()
        print("  By kind:")
        for kind in KINDS:
            n = res.kind_counts.get(kind, 0)
            if n:
                print(f"    {kind:<14} {n:>7}  {bar(n, total):<28} "
                      f"{KIND_HELP[kind]}")
        print()
        print("  By column:")
        for col, n in res.column_counts.most_common(15):
            print(f"    {col:<24} {n:>7}  {bar(n, total)}")
        if len(res.column_counts) > 15:
            print(f"    ... and {len(res.column_counts) - 15} more columns")
        print()

        cosmetic = sum(res.kind_counts.get(k, 0)
                       for k in ("whitespace", "width", "case", "number-format"))
        substantive = total - cosmetic
        print(f"  Cosmetic (formatting only): {cosmetic}")
        print(f"  Substantive (real changes): {substantive}")
        print()

        print("  First differences:")
        for d in res.diffs[:opts.show]:
            loc = f"row {d.row_no}"
            if d.key:
                loc += f" [{d.key}]"
            print(f"    {loc}  {d.col_name}  ({d.kind})")
            print(f"        A: {d.value_a!r}")
            print(f"        B: {d.value_b!r}")
        if len(res.diffs) > opts.show:
            print(f"    ... and {len(res.diffs) - opts.show} more shown in the report")
        if res.diffs_truncated:
            print(f"    ... plus {res.diffs_truncated} beyond --max-diffs "
                  f"({opts.max_diffs}), not recorded")
    print()

    print(line)
    if res.identical:
        print("RESULT: files are IDENTICAL")
    else:
        print("RESULT: files DIFFER")
    print(line)
    out.flush()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Find all differences between two CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
difference kinds:
  whitespace     same text, different leading/trailing spaces
  width          same text, fullwidth vs halfwidth (NFKC) form
  case           same text, different upper/lower case
  number-format  same number written differently (1,000 vs 1000)
  number         different numeric value
  missing        value present in one file, blank in the other
  value          different text

examples:
  python3 compare_csv.py a.csv b.csv
  python3 compare_csv.py a.csv b.csv --report diff.csv
  python3 compare_csv.py a.csv b.csv --key 請求先コード --ignore-kind width,whitespace
""")
    p.add_argument("file_a")
    p.add_argument("file_b")
    p.add_argument("--key", default="",
                   help="comma-separated key column name(s) or 1-based number(s); "
                        "match rows by key instead of by position")
    p.add_argument("--ignore-cols", default="",
                   help="comma-separated column names/numbers to skip entirely")
    p.add_argument("--ignore-kind", default="",
                   help="comma-separated diff kinds to treat as equal, "
                        "e.g. width,whitespace,case,number-format")
    p.add_argument("--report", metavar="PATH",
                   help="write a detailed per-cell CSV report to PATH")
    p.add_argument("--report-encoding", default="utf-8-sig",
                   help="encoding for the report file (default: utf-8-sig, opens "
                        "cleanly in Excel)")
    p.add_argument("--encoding", help="force input encoding for both files")
    p.add_argument("--encoding-a", help="force input encoding for file A only")
    p.add_argument("--encoding-b", help="force input encoding for file B only")
    p.add_argument("--delimiter", help="force field delimiter (default: auto-detect)")
    p.add_argument("--max-diffs", type=int, default=100000,
                   help="stop recording diffs past this many (default: 100000)")
    p.add_argument("--show", type=int, default=10,
                   help="how many example diffs to print (default: 10)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print only the final RESULT line")
    return p.parse_args(argv)


def main(argv=None):
    opts = parse_args(argv if argv is not None else sys.argv[1:])

    opts.key = [k for k in opts.key.split(",") if k.strip()]
    bad = set(k.strip() for k in opts.ignore_kind.split(",") if k.strip()) - set(KINDS)
    if bad:
        die(f"unknown diff kind(s): {', '.join(sorted(bad))}\n"
            f"       valid kinds: {', '.join(KINDS)}")
    opts.ignore_kinds = set(k.strip() for k in opts.ignore_kind.split(",") if k.strip())

    try:
        src_a = CsvSource(opts.file_a, opts.encoding_a or opts.encoding, opts.delimiter)
        src_b = CsvSource(opts.file_b, opts.encoding_b or opts.encoding, opts.delimiter)
    except OSError as exc:
        die(f"cannot open input: {exc}")

    names = [c.strip() for c in opts.ignore_cols.split(",") if c.strip()]
    opts.ignore_cols = set(key_indices(src_a.header, names)) if names else set()

    res = Result()
    compare_headers(res, src_a.header, src_b.header)
    try:
        if opts.key:
            compare_by_key(res, opts, src_a, src_b)
        else:
            compare_positional(res, opts, src_a, src_b)
    except UnicodeDecodeError as exc:
        die(f"decoding failed mid-file ({exc}).\n"
            f"       Re-run with an explicit --encoding.")
    except csv.Error as exc:
        die(f"malformed CSV: {exc}")
    finally:
        src_a.close()
        src_b.close()

    if opts.report:
        try:
            write_report(opts.report, res, opts, opts.report_encoding)
        except (OSError, UnicodeEncodeError) as exc:
            die(f"cannot write report to {opts.report!r}: {exc}\n"
                f"       Try --report-encoding utf-8.")

    if opts.quiet:
        print("RESULT: files are IDENTICAL" if res.identical else "RESULT: files DIFFER")
    else:
        print_summary(res, opts, src_a, src_b, opts.file_a, opts.file_b)
        if opts.report:
            print(f"Detailed report written to: {opts.report}")

    return 0 if res.identical else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except BrokenPipeError:
        sys.exit(2)
