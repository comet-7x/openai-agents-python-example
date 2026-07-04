#!/usr/bin/env python3
"""Recalculate every formula in an .xlsx workbook and report Excel error values.

LibreOffice recalculates all formulas whenever it opens and re-saves a
workbook, so round-tripping the file through `soffice --convert-to xlsx`
is enough to force recalculation - no macro or UNO scripting required.

Usage:
    python recalc.py <path-to-xlsx> [timeout_seconds]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openpyxl import load_workbook

_ERROR_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}
_DEFAULT_TIMEOUT = 60.0
_SOFFICE = shutil.which("soffice") or "soffice"


def _recalculate(path: Path, timeout: float) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp)
        result = subprocess.run(  # noqa: S603
            [
                _SOFFICE,
                "--headless",
                "--norestore",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(outdir),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        converted = outdir / path.name
        if result.returncode != 0 or not converted.is_file():
            raise RuntimeError(
                f"LibreOffice conversion failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        shutil.copy(converted, path)


def _scan_errors(path: Path) -> dict[str, object]:
    values_wb = load_workbook(path, data_only=True)
    formula_wb = load_workbook(path, data_only=False)

    total_formulas = 0
    error_counts: dict[str, int] = {}
    error_locations: dict[str, list[str]] = {}

    for sheet_name in formula_wb.sheetnames:
        formula_sheet = formula_wb[sheet_name]
        values_sheet = values_wb[sheet_name]
        for row in formula_sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    total_formulas += 1
                value = values_sheet[cell.coordinate].value
                if isinstance(value, str) and value in _ERROR_VALUES:
                    error_counts[value] = error_counts.get(value, 0) + 1
                    error_locations.setdefault(value, []).append(f"{sheet_name}!{cell.coordinate}")

    total_errors = sum(error_counts.values())
    report: dict[str, object] = {
        "status": "errors_found" if total_errors else "success",
        "total_errors": total_errors,
        "total_formulas": total_formulas,
    }
    if total_errors:
        report["error_summary"] = {
            code: {"count": count, "locations": error_locations[code]}
            for code, count in error_counts.items()
        }
    return report


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python recalc.py <path-to-xlsx> [timeout_seconds]", file=sys.stderr)
        return 2

    path = Path(sys.argv[1]).resolve()
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_TIMEOUT

    if not path.is_file():
        print(json.dumps({"status": "error", "message": f"file not found: {path}"}))
        return 1

    try:
        _recalculate(path, timeout)
    except subprocess.TimeoutExpired:
        print(json.dumps({"status": "error", "message": f"LibreOffice timed out after {timeout}s"}))
        return 1
    except RuntimeError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1

    report = _scan_errors(path)
    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "errors_found" else 0


if __name__ == "__main__":
    raise SystemExit(main())
