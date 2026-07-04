---
name: xlsx
description: Read, create, and edit Excel spreadsheets (.xlsx). Load this when a task's input or output is a spreadsheet - building a model, adding formulas or formatting, analyzing tabular data, or fixing an existing workbook.
---
# Working with Excel spreadsheets

## Two libraries, two jobs

- **pandas** - fast for reading data, bulk analysis, and simple exports. Doesn't preserve
  formulas or formatting on write.
- **openpyxl** - the right tool whenever formulas, cell formatting, multiple sheets, or an
  existing template are involved. Use it for anything the user will open in Excel afterward.

```python
import pandas as pd
df = pd.read_excel("file.xlsx")          # first sheet
sheets = pd.read_excel("file.xlsx", sheet_name=None)  # dict of all sheets
```

```python
from openpyxl import Workbook, load_workbook

wb = Workbook()                          # new file
wb = load_workbook("existing.xlsx")      # edit existing - preserves formulas/formatting

sheet = wb.active
sheet["A1"] = "Revenue"
sheet["B1"] = "=SUM(B2:B10)"
wb.save("output.xlsx")
```

Cell indices in openpyxl are 1-based (row=1, col=1 is A1). `load_workbook(..., data_only=True)`
reads the last-cached formula *result* instead of the formula text - but saving a workbook opened
that way discards the formulas permanently, so only use `data_only=True` for read-only inspection.

## Always compute with formulas, not Python

Write `=SUM(B2:B10)` into the cell, not the Python-computed sum. A hardcoded number stops updating
the moment the source data changes; a formula stays live. This applies to every calculation -
totals, ratios, growth rates, everything derived from other cells.

## Recalculating formulas

openpyxl writes formulas as plain text - it does not evaluate them, so a freshly saved workbook has
no cached results until something opens and recalculates it. Use `scripts/recalc.py` after saving
any file that contains formulas:

```bash
python scripts/recalc.py output.xlsx [timeout_seconds]
```

It round-trips the file through headless LibreOffice (which recalculates on save), then scans every
cell for Excel error values and reports them as JSON:

```json
{
  "status": "errors_found",
  "total_errors": 1,
  "total_formulas": 12,
  "error_summary": {
    "#DIV/0!": {"count": 1, "locations": ["Sheet1!B5"]}
  }
}
```

`status` is `"success"` when there are no errors, exit code 0. On `"errors_found"` (exit code 1),
fix the referenced cells and re-run - don't hand back a workbook with unresolved `#REF!`,
`#DIV/0!`, `#VALUE!`, `#N/A`, or `#NAME?` errors.

## Merged cells and messy used-ranges

Real-world workbooks (multi-row hierarchical headers, category labels spanning
several data rows, only *some* rows merged while others aren't) need two things
a plain `pd.read_excel` or naive openpyxl loop won't give you:

1. **Merged cells only store a value in their top-left cell.** Every other
   cell in a merged range reads as `None`. Reading a table with a merged
   header row naively produces `None`s for most of that row.
2. **`ws.dimensions` / `ws.max_row` / `ws.max_column` can be wildly wrong.**
   A single stray formatting-only merge (e.g. one that spans to row
   1,048,576) makes openpyxl report a sheet as far larger than its actual
   data. Trusting it and iterating the full reported range can mean walking
   *millions* of empty cells - loading such a file in normal (non-`read_only`)
   mode can also take tens of seconds by itself, because openpyxl eagerly
   materializes every cell object in that inflated range.

Use `scripts/read_merged.py` for this - it detects the real data extent by
scanning bounded, and reads merged-cell ranges directly from the sheet's XML
(fast) rather than through a normal openpyxl load (slow on files like this):

```bash
# List every sheet with its real extent and merge count - run this first
python scripts/read_merged.py workbook.xlsx

# Dump one sheet as a 2D JSON array with every merged cell forward-filled
python scripts/read_merged.py workbook.xlsx "Sheet Name"
```

If you need this logic inside a larger Python script rather than as a
subprocess call, `read_sheet_filled(path, sheet_name)` in that script is
directly importable and returns the same forward-filled 2D list.

## Cross-tab / matrix sheets - unpivot before reading

`read_merged.py` forward-fills merges but still hands you the raw grid. When that
grid is a **cross-tab** - categories running across the top as a multi-row column
header (banner rows like `物种` / `部位`, one category merged across several
columns), metrics running down the side, values at each intersection - a
forward-filled grid is still nearly unreadable for a model: a column's identity is
split across several non-adjacent header rows, and a small sheet becomes thousands
of sparse cells. Signs you're looking at one: `read_merged.py` reports a wide sheet
(dozens/hundreds of columns) with many merges, and the top rows are repeated
category labels rather than a single header row.

Don't feed that to the model. Unpivot it to **tidy long records** first - one
record per (column-identity × metric):

```bash
# One universal step - infers the structure from the data and emits JSONL.
# The spec it inferred is printed to stderr so you can see what it did.
python scripts/normalize.py workbook.xlsx "Sheet Name" --auto
```

```json
{"物种": "杜氏枪乌贼", "部位": "酮体", "加工方式": "...", "metric": "水分", "value": 0.0825}
```

`--auto` resolves the tricky ambiguities from the data itself: how deep the header
band is, which row holds each axis's values, and - crucially - whether a merged
banner span means "one category grouping several columns" (species) or "each
column has its own label" (body part). It also drops reference/spacer columns and,
if the sheet turns out to be an ordinary table (no cross-tab axes), emits nothing
and tells you to use `read_merged.py` instead.

Use the escape hatches only when `--auto` guesses wrong on a pathological sheet
(e.g. duplicate banner labels, an unusual value-row offset):

```bash
python scripts/normalize.py workbook.xlsx "Sheet" --detect       # show inferred spec, don't emit
python scripts/normalize.py workbook.xlsx "Sheet" --spec '{...}'  # unpivot with a corrected spec
```

Copy the spec from `--detect`, fix the wrong field (see the spec format in the
script's header docstring), and pass it back via `--spec`. This stays a runtime
decision - never commit a per-file spec.

## Common mistakes to check for

- **Excel is 1-indexed, pandas is 0-indexed.** DataFrame row 5 is Excel row 6 (plus 1 more if
  there's a header row).
- **NaN from pandas breaks formulas.** Check with `pd.notna()` before writing a value into a cell.
- **Cross-sheet references** need the `SheetName!A1` form, not just `A1`.
- **Division by zero and empty denominators** - guard them before writing a formula that divides.
- **Existing templates win.** If editing a file with established formatting conventions, match
  them exactly rather than imposing a different style.
