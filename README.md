# comparecsv

Two scripts:

- **`compare_csv.py`** — finds every difference between two CSV files.
- **`export_report.py`** — renders that difference report as **PDF** and **PNG**.

---

`compare_csv.py` finds **every** difference between two CSV files.

It answers exactly the three questions you need:

1. Do both files have the same number of columns (and the same header names)?
2. Do they have the same number of rows?
3. Is every cell identical — and if not, precisely which ones differ?

Pure Python 3 standard library. No `pip install`, no dependencies.

---

## Quick start

```bash
python3 compare_csv.py FILE_A FILE_B
```

For your delayed-payment files:

```bash
python3 compare_csv.py "20260717-0942-都市ガス遅延金一覧表.CSV" "遅延計算結果_20260717.csv" --report 差分レポート.csv
```

`--report` writes every difference to a CSV you can open directly in Excel.

---

## Why not just use `diff`?

Running `diff` on your two files reports **all 814 lines as different**, which is
useless. The reason is that the second file wraps every field in quotes:

```
A:  130,都市ガス,0,,100000093,樫村　まゆみ,廃止手続中,...
B:  "130","都市ガス","0","","100000093","樫村　まゆみ","",...
```

`compare_csv.py` parses the CSV properly, so quoting, line endings, and
encoding differences never masquerade as data changes. Of those 814 lines,
only **19 cells** actually differ.

---

## Differences are classified, not just listed

The most useful feature: each difference is labelled by *kind*, so genuine data
changes are not buried under formatting noise.

| Kind | Meaning | Significance |
|---|---|---|
| `whitespace` | Same text, different leading/trailing spaces (incl. full-width `　`) | cosmetic |
| `width` | Same text, full-width vs half-width — `ABC` vs `ＡＢＣ` | cosmetic |
| `case` | Same text, different upper/lower case | cosmetic |
| `number-format` | Same number written differently — `1,000` vs `1000` | cosmetic |
| `number` | **Different numeric value** — `25` vs `24` | real |
| `missing` | Value in one file, blank in the other | real |
| `value` | Genuinely different text | real |

The summary totals cosmetic vs substantive changes separately.

To hide cosmetic noise entirely and see only real changes:

```bash
python3 compare_csv.py A.CSV B.CSV --ignore-kind whitespace,width,case,number-format
```

---

## Matching rows: by position or by key

**By position (default).** Row *n* of A is compared against row *n* of B. This
is right when both files are exports of the same list in the same order — which
is the case for your files.

**By key.** Use when rows may be reordered, inserted, or deleted:

```bash
python3 compare_csv.py A.CSV B.CSV --key 請求先コード
```

Rows are then matched on that column's value regardless of position, and rows
present in only one file are reported separately.

> **Note for your files:** `請求先コード` is *not* unique — 813 rows contain only
> 578 distinct values. As a single key it would mismatch duplicate rows. Adding
> `検針日` makes it unique (813/813), so use:
>
> ```bash
> python3 compare_csv.py A.CSV B.CSV --key 請求先コード,検針日
> ```
>
> Otherwise stay with the positional default, which is correct here since both
> files are already in identical order.

---

## Options

| Option | Purpose |
|---|---|
| `--report PATH` | Write a detailed per-cell CSV report (opens in Excel) |
| `--key COLS` | Match rows by column name(s)/number(s) instead of position |
| `--ignore-kind KINDS` | Treat the listed difference kinds as equal |
| `--ignore-cols COLS` | Skip columns entirely (e.g. a timestamp column) |
| `--encoding ENC` | Force input encoding (default: auto-detect) |
| `--encoding-a` / `--encoding-b` | Force encoding for one file only |
| `--delimiter CHAR` | Force the delimiter (default: auto-detect) |
| `--report-encoding ENC` | Report encoding (default `utf-8-sig`, Excel-safe) |
| `--show N` | How many example differences to print (default 10) |
| `--max-diffs N` | Cap on recorded differences (default 100000) |
| `-q`, `--quiet` | Print only the final RESULT line |

Encoding is auto-detected (your files are `cp932`/Shift-JIS), as is the
delimiter. You normally do not need to set either.

### Exit codes

`0` identical · `1` differences found · `2` error

This makes it easy to use in a script:

```bash
if python3 compare_csv.py A.CSV B.CSV -q; then
    echo "OK - no differences"
else
    echo "Differences found - check the report"
fi
```

---

## Exporting to PDF and PNG

`export_report.py` turns the CSV report into a styled, colour-coded document.

```bash
python3 export_report.py 差分レポート.csv
```

With no output paths given it writes `差分レポート.pdf` and `差分レポート.png`
next to the input. To control them explicitly:

```bash
python3 export_report.py 差分レポート.csv --pdf out.pdf --png out.png \
    --title "都市ガス遅延金 CSV差分レポート"
```

The full routine, from raw files to PDF + PNG:

```bash
python3 compare_csv.py A.CSV B.CSV --report 差分レポート.csv
python3 export_report.py 差分レポート.csv --title "CSV差分レポート"
```

The rendered document shows summary cards (total / substantive / cosmetic, plus
a count per kind), a legend explaining each kind, and the difference table with
A-values in blue, B-values in orange, and a colour-coded kind badge per row.
Blank cells are marked *(blank)* so a missing value is never confused with a
space. Summary counts always reflect the **whole** report, even when
`--max-rows` clips the displayed table.

### Options

| Option | Purpose |
|---|---|
| `--pdf PATH` / `--png PATH` | Output paths (default: alongside the input) |
| `--title TEXT` | Document heading |
| `--orientation` | `landscape` (default) or `portrait` |
| `--max-rows N` | Cap on rendered table rows (default 400) |
| `--width N` | PNG width in CSS px (default 1500) |
| `--scale N` | PNG device scale factor (default 2.0, i.e. retina) |
| `--encoding ENC` | Force input encoding (default: auto-detect) |
| `--browser PATH` | Path to Chrome/Chromium |
| `--keep-html PATH` | Also save the intermediate HTML |

### Requirements

Rendering uses **Google Chrome or Chromium** headlessly — already present on
this machine, and found automatically. Japanese text renders correctly via the
installed `Noto Sans CJK JP` font.

The PDF is A4 with the table header repeated on every page. The PNG is a single
full-height image, auto-cropped to the content.

`export_report.py` also renders any ordinary CSV as a plain table, so it works
as a general CSV-to-PDF/PNG tool:

```bash
python3 export_report.py any_table.csv --title "My table"
```

---

## Large files

Rows are streamed and compared one pair at a time, so memory stays flat
regardless of file size. Measured on this machine:

| Input | Time | Peak memory |
|---|---|---|
| 813 rows | instant | ~16 MB |
| 500,000 rows (36 MB total) | 1.2 s | ~19 MB |

Memory does not grow with row count in positional mode. `--key` mode indexes
one file in memory, so it uses more on very large inputs; prefer the positional
default when row order is stable.

---

## Current result for the files in this repository

```
STRUCTURE
  [OK]   column count identical (13 columns)
  [OK]   header names identical
  [OK]   row count identical (813 data rows)
  [OK]   every row has the expected number of fields

CELL VALUES
  [DIFF] 19 differing cell(s) in 19 of 813 compared rows

    width                5   same text, fullwidth vs halfwidth
    number               2   different numeric value
    missing             12   value present in one file, blank in the other

  By column:
    ステータス            12
    請求先顧客名           5
    今回遅延金額           2

  Cosmetic (formatting only): 5
  Substantive (real changes): 14
```

**Interpretation:**

- **Structure is fully identical** — same 13 columns, same headers, same 813 rows.
- **`ステータス` (12 cells)** — the 一覧表 has `廃止手続中`, the 計算結果 is blank.
  A whole status column is being dropped in the calculation output.
- **`今回遅延金額` (2 cells)** — real amount discrepancies, the ones that matter:
  - row 248: `25` → `24`
  - row 417: `26` → `23`
- **`請求先顧客名` (5 cells)** — full-width/half-width only (`CHAN HAI YING` vs
  `ＣＨＡＮ　ＨＡＩ　ＹＩＮＧ`). Same names, different character forms. Cosmetic.
