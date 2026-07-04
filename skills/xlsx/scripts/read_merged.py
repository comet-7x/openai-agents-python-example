#!/usr/bin/env python3
"""Read an .xlsx sheet as a clean 2D grid, handling two common real-world problems:

1. Merged cells: openpyxl only stores a value in the top-left cell of a merged
   range - every other cell in it reads as None. This forward-fills each
   merged range's value into every cell it covers.
2. Inflated used-range: a single stray formatting-only merge (e.g. spanning to
   row 1,048,576) can make openpyxl report a sheet as far larger than its
   actual data - naive iteration then walks millions of empty cells. This
   detects the real data extent instead of trusting `ws.dimensions`.

Reading merge ranges via a normal openpyxl load is what's slow on files like
this (tens of seconds), because it eagerly materializes every cell in the
inflated range. Reading them directly from the sheet XML is not.

Usage:
    python read_merged.py <file.xlsx>                    # list sheets + real extent + merge count
    python read_merged.py <file.xlsx> <sheet_name>        # dump that sheet as JSON rows
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def merges_by_sheet(path: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """Merged-cell ranges per sheet, read straight from the workbook XML."""
    with zipfile.ZipFile(path) as z:
        # Parses the xlsx's own internal XML, not externally supplied data - the
        # caller already has full shell access to this file.
        wb_xml = ET.fromstring(z.read("xl/workbook.xml"))  # noqa: S314
        rels_xml = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))  # noqa: S314
        rid_to_target = {
            r.get("Id"): r.get("Target")
            for r in rels_xml
            if r.get("Id") is not None and r.get("Target") is not None
        }

        out: dict[str, list[tuple[int, int, int, int]]] = {}
        sheets_el = wb_xml.find("m:sheets", _NS)
        for sheet in sheets_el if sheets_el is not None else []:
            name = sheet.get("name") or ""
            rid = sheet.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            target = rid_to_target[rid]
            if target is None:
                continue
            sheet_path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
            sheet_xml = ET.fromstring(z.read(sheet_path))  # noqa: S314
            merges_el = sheet_xml.find("m:mergeCells", _NS)
            ranges = [
                tuple(range_boundaries(ref))
                for mc in (merges_el or [])
                if (ref := mc.get("ref")) is not None
            ]
            out[name] = ranges  # pyright: ignore[reportArgumentType]
        return out


def real_extent(ws: object, max_empty_run: int = 200, max_col_cap: int = 1000) -> tuple[int, int]:
    """The sheet's actual (max_row, max_col) with data, bounding a possibly
    wildly inflated reported range. Stops scanning after `max_empty_run`
    consecutive empty rows once data has been seen."""
    last_data_row = 0
    max_col = 0
    empty_run = 0
    for row_idx, row in enumerate(ws.iter_rows(max_col=max_col_cap), start=1):  # type: ignore[attr-defined]
        row_has_data = False
        for col_idx, cell in enumerate(row, start=1):
            if cell.value is not None:
                row_has_data = True
                max_col = max(max_col, col_idx)
        if row_has_data:
            last_data_row = row_idx
            empty_run = 0
        else:
            empty_run += 1
            if last_data_row and empty_run > max_empty_run:
                break
    return last_data_row, max_col


def read_sheet_filled(path: str, sheet_name: str) -> list[list[object]]:
    """Sheet contents as a 2D list, merged cells forward-filled, bounded to
    real data extent."""
    merges = merges_by_sheet(path).get(sheet_name, [])
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name]
        max_row, max_col = real_extent(ws)
        rows_raw: list[list[object]] = [
            [cell.value for cell in row] for row in ws.iter_rows(max_row=max_row, max_col=max_col)
        ]
    finally:
        wb.close()

    for min_col, min_row, max_col_m, max_row_m in merges:
        if min_row > max_row or min_col > max_col:
            continue
        source_value = rows_raw[min_row - 1][min_col - 1]
        for r in range(min_row, min(max_row_m, max_row) + 1):
            for c in range(min_col, min(max_col_m, max_col) + 1):
                if (r, c) != (min_row, min_col):
                    rows_raw[r - 1][c - 1] = source_value
    return rows_raw


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python read_merged.py <file.xlsx> [sheet_name]", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    if not path.is_file():
        print(json.dumps({"status": "error", "message": f"file not found: {path}"}))
        return 1

    if len(sys.argv) < 3:
        wb = load_workbook(str(path), read_only=True)
        try:
            all_merges = merges_by_sheet(str(path))
            summary = []
            for name in wb.sheetnames:
                max_row, max_col = real_extent(wb[name])
                summary.append(
                    {
                        "sheet": name,
                        "real_max_row": max_row,
                        "real_max_col": max_col,
                        "merged_ranges": len(all_merges.get(name, [])),
                    }
                )
        finally:
            wb.close()
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    sheet_name = sys.argv[2]
    rows = read_sheet_filled(str(path), sheet_name)
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
