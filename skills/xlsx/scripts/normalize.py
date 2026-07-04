#!/usr/bin/env python3
"""Turn a cross-tab / matrix .xlsx sheet into tidy long-format records.

Many real-world spreadsheets are *cross-tabs*: categories run across the top as
a multi-row column header (often with banner rows like `物种` / `部位` and merged
group spans), metrics run down the side, and each cell is a value at the
intersection. Feeding that raw grid to an LLM is hopeless - the identity of a
column is spread across several non-adjacent header rows, merged groups leave
most cells empty, and a modest sheet becomes thousands of sparse cells.

This script unpivots such a layout into one record per (column-identity x metric):

    {"物种": "杜氏枪乌贼", "部位": "酮体", "加工方式": "...", "metric": "水分", "value": 0.0825}

Usage - one universal procedure, same for every file:

    python normalize.py <file.xlsx> <sheet> --auto [--format jsonl|csv] [--limit N]
        Infer the structure from the data and emit tidy records. This is the
        default path. The spec it inferred is echoed to stderr so you can see
        what it did. If the sheet has no cross-tab axes (already tabular), it
        emits nothing and tells you to use read_merged.py.

    python normalize.py <file.xlsx> <sheet> --detect
        Show the inferred structure + spec without unpivoting. Use to inspect
        before committing, or to copy the spec and tweak it.

    python normalize.py <file.xlsx> <sheet> --spec spec.json
        Override: unpivot with a hand-corrected spec (file path or inline JSON),
        for the rare pathological sheet where --auto guesses wrong.

Why inference can be automatic: header geometry is not uniform even within one
workbook - the value row for an axis may sit directly under its banner or a row
lower, and some axes group columns by a *banner-row merge* (one species spanning
several columns) while others carry a distinct value in every column (body part).
That ambiguity is resolved from the data itself (see `_infer_group_by`), not from
a fixed rule, so --auto handles the common cases without tuning; --spec is the
escape hatch, not the normal path.

Spec format (all rows/cols are 1-based, matching Excel):

    {
      "data_start_row": 8,           # first row of metric data
      "data_col_start": 2,           # first data column (right of the label cols)
      "label_cols": [                # leading columns that identify each ROW
        {"name": "metric", "col": 1}
      ],
      "axes": [                      # header layers that identify each COLUMN
        {"name": "物种",     "value_row": 2, "group_by": {"merge_row": 1}},
        {"name": "部位",     "value_row": 4, "group_by": "self"},
        {"name": "加工方式", "value_row": 6, "group_by": "self"}
      ],
      "sections": {"field": "section"}   # optional; omit to disable
    }

  group_by "self":            value comes straight from (value_row, col), filled
                              only within that row's own merges. Use when every
                              column carries its own label.
  group_by {"merge_row": M}:  columns are grouped by the merge spans on row M;
                              the value is taken from value_row at each group's
                              left edge. Use when a wide banner merge (e.g. one
                              species spanning several body-part columns) defines
                              the grouping but the value sits a row below.

  sections: a data row whose first label col has text while every data column is
  empty is treated as a section header; its label is attached to following rows
  under `sections.field` and the row itself is not emitted.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

# Reuse the extent/merge helpers already battle-tested in read_merged.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_merged import merges_by_sheet, real_extent


def _extent(path: str, sheet: str) -> tuple[int, int]:
    """Real (max_row, max_col) with data. Computed in read_only mode - normal
    mode would iterate up to a stray formatting merge's inflated max_row (often
    1,048,576) and hang. Random cell access afterwards stays bounded by this."""
    wb = load_workbook(path, read_only=True)
    try:
        return real_extent(wb[sheet])
    finally:
        wb.close()


def _load_grid(path: str, sheet: str) -> tuple[list[list], int, int]:
    """Read the sheet's real data region into an in-memory 1-based grid.

    Everything here is done in read_only mode. Opening the same file in normal
    mode is *not* an option: a stray formatting merge spanning to row 1,048,576
    (common in these workbooks) makes openpyxl materialize millions of MergedCell
    objects on load - a ~40s stall - even though the actual data is tiny. We cap
    the region with _extent first, then stream just that rectangle."""
    max_row, max_col = _extent(path, sheet)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        grid: list[list] = [[None] * (max_col + 1)]  # row 0 padding; rows/cols are 1-based
        for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
            vals = [None, *[c.value for c in row]]
            if len(vals) < max_col + 1:
                vals.extend([None] * (max_col + 1 - len(vals)))
            grid.append(vals)
    finally:
        wb.close()
    return grid, max_row, max_col


def _cell(grid: list[list], row: int, col: int) -> object:
    if 1 <= row < len(grid) and 1 <= col < len(grid[row]):
        return grid[row][col]
    return None


def _clean(v: object) -> object:
    """Trim whitespace/newlines from string cells; pass others through."""
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def _merge_at(merges: list[tuple[int, int, int, int]], row: int, col: int):
    """The (min_col, min_row, max_col, max_row) merge covering (row, col), or None."""
    for min_col, min_row, max_col, max_row in merges:
        if min_row <= row <= max_row and min_col <= col <= max_col:
            return (min_col, min_row, max_col, max_row)
    return None


def _looks_numeric(v: object) -> bool:
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("±", " ").split()[:1]
        if not s:
            return False
        try:
            float(s[0])
            return True
        except ValueError:
            return False
    return False


# --------------------------------------------------------------------------- #
# Structure inference
# --------------------------------------------------------------------------- #
def _infer_group_by(
    grid: list[list],
    merges: list[tuple[int, int, int, int]],
    banner_row: int,
    value_row: int,
    data_col_start: int,
    max_col: int,
) -> object:
    """Decide whether an axis groups columns by the banner-row merge, or carries
    a distinct value in every column, by looking at how values are distributed
    within each banner-merge span on the value row:

      value only at a span's left edge, rest empty -> group  ({"merge_row": R})
      distinct value in (most of) each column        -> per-column  ("self")

    Only spans wider than one column are informative; single-column spans can't
    disambiguate. Ties and the no-wide-span case fall back to "self", the safer
    reading (it never fabricates a grouping that isn't there)."""
    group_votes = 0
    self_votes = 0
    seen_spans = {(mc[0], mc[2]) for mc in merges if mc[1] == banner_row}
    for min_col, max_col_span in sorted(seen_spans):
        lo = max(min_col, data_col_start)
        hi = min(max_col_span, max_col)
        if hi - lo < 1:  # need at least 2 columns to tell grouping from per-column
            continue
        non_empty = sum(
            1 for c in range(lo, hi + 1) if _clean(_cell(grid, value_row, c)) is not None
        )
        if non_empty <= 1:
            group_votes += 1
        else:
            self_votes += 1
    if group_votes > self_votes:
        return {"merge_row": banner_row}
    return "self"


# --------------------------------------------------------------------------- #
# Unpivot
# --------------------------------------------------------------------------- #
def _axis_value(
    grid: list[list],
    merges: list[tuple[int, int, int, int]],
    axis: dict,
    col: int,
) -> object:
    value_row = int(axis["value_row"])
    group_by = axis.get("group_by", "self")

    if group_by == "self":
        m = _merge_at(merges, value_row, col)
        src_col = m[0] if m else col
        return _clean(_cell(grid, value_row, src_col))

    # {"merge_row": M}: grouping defined by the banner-row merge; value one row below.
    merge_row = int(group_by["merge_row"])
    m = _merge_at(merges, merge_row, col)
    src_col = m[0] if m else col
    val = _clean(_cell(grid, value_row, src_col))
    if val is None:  # value row not merged - fall back to this column's own cell
        val = _clean(_cell(grid, value_row, col))
    return val


def unpivot(path: str, sheet: str, spec: dict) -> list[dict]:
    merges = merges_by_sheet(path).get(sheet, [])
    grid, max_row, max_col = _load_grid(path, sheet)

    label_cols = spec["label_cols"]
    axes = spec["axes"]
    if spec.get("data_start_row") is None or not axes:
        return []  # not a cross-tab: no metric grid, or no column axes to unpivot
    data_start_row = int(spec["data_start_row"])
    data_col_start = int(spec.get("data_col_start", max(lc["col"] for lc in label_cols) + 1))
    section_field = (spec.get("sections") or {}).get("field")

    # Pre-resolve each data column's identity once. A column whose axis identity
    # is entirely null is a label/reference/spacer column (e.g. a "recommended
    # intake" column to the left of the real data), not a measurement column -
    # drop it so its values don't surface as records with null dimensions.
    col_identity: dict[int, dict] = {}
    for c in range(data_col_start, max_col + 1):
        identity = {ax["name"]: _axis_value(grid, merges, ax, c) for ax in axes}
        if any(v is not None for v in identity.values()):
            col_identity[c] = identity

    records: list[dict] = []
    current_section: object = None
    last_label: dict[str, object] = {}

    for r in range(data_start_row, max_row + 1):
        # Row-identifying label columns (fill down through vertical merges).
        row_labels: dict[str, object] = {}
        for lc in label_cols:
            m = _merge_at(merges, r, lc["col"])
            src_row = m[1] if m else r
            v = _clean(_cell(grid, src_row, lc["col"]))
            if v is not None:
                last_label[lc["name"]] = v
            row_labels[lc["name"]] = v if v is not None else last_label.get(lc["name"])

        data_cells = [(_clean(_cell(grid, r, c)), c) for c in col_identity]
        has_data = any(v is not None for v, _ in data_cells)

        first_label = row_labels.get(label_cols[0]["name"])
        # Section header: first label present, everything else empty.
        if (
            section_field
            and first_label is not None
            and not has_data
            and all(row_labels.get(lc["name"]) in (None, first_label) for lc in label_cols[1:])
        ):
            current_section = first_label
            last_label = {}  # a new section restarts fill-down
            continue

        if not has_data:
            continue

        for value, c in data_cells:
            if value is None:
                continue
            rec: dict[str, object] = dict(col_identity[c])
            rec.update(row_labels)
            if section_field and current_section is not None:
                rec[section_field] = current_section
            rec["value"] = value
            records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# Detect
# --------------------------------------------------------------------------- #
_DATA_COL0 = 2  # header rows are classified over columns from here rightward (col 1 = labels)


def _build_spec(grid: list[list], merges: list, max_row: int, max_col: int) -> dict:
    """Infer a full unpivot spec from the sheet's *structure*, with no hardcoded
    vocabulary - so it works regardless of language or domain: the first data row,
    the header rows above it, which of those are axes, each axis's group_by
    (resolved from the data by `_infer_group_by`), and each axis's name.

    Header rows are classified over the data columns by how many distinct values
    they carry:
      * exactly one distinct value repeated across the width -> a *banner* row; it
        names the axis (e.g. a merged "物种" / "Region" label) and the next
        many-valued row below it holds that axis's per-column values.
      * two or more distinct values -> a *value* row = an axis level directly.
    A value row with no banner above it becomes an axis named `col_level_N`.
    This is the universal, no-tuning path; `--spec` overrides a wrong guess."""
    # First data row: first row with >= 2 numeric-looking cells to the right of col 1.
    data_start_row = None
    for r in range(1, max_row + 1):
        numeric = sum(
            1 for c in range(_DATA_COL0, max_col + 1) if _looks_numeric(_cell(grid, r, c))
        )
        if numeric >= 2 and _clean(_cell(grid, r, 1)) is not None:
            data_start_row = r
            break

    header_top = (data_start_row - 1) if data_start_row else max_row

    # Classify every header row over the data columns. A *banner* row is low
    # cardinality dominated by one repeated label (e.g. a merged "物种" / "Region"
    # spanning the width - possibly with a stray spelling variant or two); a
    # *value* row spreads many distinct values across the columns. Dominance, not
    # strict uniqueness, is the test - real banners often carry minor variants
    # ("部位", "部位 mg·kg⁻¹") that a `== 1 distinct` rule would misread.
    profile: dict[int, dict] = {}
    for r in range(1, header_top + 1):
        vals = [
            str(v)
            for c in range(_DATA_COL0, max_col + 1)
            if (v := _clean(_cell(grid, r, c))) is not None
        ]
        if not vals:
            profile[r] = {"kind": "empty", "label": None}
            continue
        freq = Counter(vals)
        top_val, top_n = freq.most_common(1)[0]
        # One label covering most of the row = a banner, however many minor
        # spelling variants tag along (e.g. "部位" plus "部位mg/100mg"). A value
        # row spreads its cells across many roughly-equally-frequent values.
        dominant = top_n / len(vals) >= 0.6
        profile[r] = {
            "kind": "banner" if dominant else "value",
            "label": top_val if dominant else None,
        }

    banner_rows = [r for r, p in profile.items() if p["kind"] == "banner"]
    value_rows = [r for r, p in profile.items() if p["kind"] == "value"]

    # A real cross-tab MUST have at least one banner row - a dominant-label header
    # naming a category axis. A plain tidy table (single header row of distinct
    # field names, data below) has none, and must NOT be unpivoted - otherwise its
    # lone header row gets read as an axis and every cell explodes into a record.
    axes: list[dict] = []
    consumed: set[int] = set()
    if banner_rows:
        for br in banner_rows:  # pair each banner with the nearest value row below it
            vr = next((r for r in value_rows if r > br and r not in consumed), None)
            if vr is None:
                continue
            consumed.add(vr)
            axes.append(
                {
                    "name": profile[br]["label"],
                    "banner_row": br,
                    "value_row": vr,
                    "group_by": _infer_group_by(grid, merges, br, vr, _DATA_COL0, max_col),
                }
            )
        for vr in value_rows:  # value rows with no banner above -> their own axis
            if vr not in consumed:
                axes.append({"name": None, "banner_row": None, "value_row": vr, "group_by": "self"})

    axes.sort(key=lambda a: a["value_row"])
    used: dict[str, int] = {}  # give unnamed axes a name and de-duplicate collisions
    for i, a in enumerate(axes, start=1):
        name = a["name"] or f"col_level_{i}"
        used[name] = used.get(name, 0) + 1
        a["name"] = name if used[name] == 1 else f"{name}_{used[name]}"

    tidy_looking = not banner_rows  # no dominant-label header row => an ordinary table
    spec = {
        "data_start_row": data_start_row,
        "data_col_start": _DATA_COL0,
        "label_cols": [{"name": "metric", "col": 1}],
        "axes": [{k: a[k] for k in ("name", "value_row", "group_by")} for a in axes],
        "sections": {"field": "section"},
    }
    return {"spec": spec, "axes_detail": axes, "looks_already_tidy": tidy_looking}


def detect(path: str, sheet: str) -> dict:
    merges = merges_by_sheet(path).get(sheet, [])
    grid, max_row, max_col = _load_grid(path, sheet)
    built = _build_spec(grid, merges, max_row, max_col)
    tidy = built["looks_already_tidy"]
    return {
        "sheet": sheet,
        "real_max_row": max_row,
        "real_max_col": max_col,
        "merged_ranges": len(merges),
        "axes_detail": built["axes_detail"],
        "looks_already_tidy": tidy,
        "note": (
            "Row 1 looks like plain column headers and no banner axes were found - this sheet "
            "may already be tabular; use read_merged.py instead of unpivoting."
            if tidy
            else "group_by was inferred from the data. Run --auto to unpivot with this spec, or "
            "pass a corrected spec via --spec (e.g. add a reference label_col, or fix an axis)."
        ),
        "spec": built["spec"],
    }


def auto_unpivot(path: str, sheet: str) -> tuple[dict, list[dict]]:
    """The universal one-shot path: infer the spec, then unpivot with it.
    Returns (spec_used, records)."""
    merges = merges_by_sheet(path).get(sheet, [])
    grid, max_row, max_col = _load_grid(path, sheet)
    built = _build_spec(grid, merges, max_row, max_col)
    if built["looks_already_tidy"] or not built["spec"]["axes"]:
        return built["spec"], []
    return built["spec"], unpivot(path, sheet, built["spec"])


def _emit(records: list[dict], fmt: str, limit: int | None) -> None:
    if limit is not None:
        records = records[:limit]
    if fmt == "csv":
        if not records:
            return
        fields: list[str] = []
        for rec in records:
            for k in rec:
                if k not in fields:
                    fields.append(k)
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    else:  # jsonl
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("file")
    ap.add_argument("sheet")
    ap.add_argument("--auto", action="store_true", help="infer the spec and unpivot in one step")
    ap.add_argument(
        "--detect", action="store_true", help="report inferred structure + spec, don't unpivot"
    )
    ap.add_argument("--spec", help="override: spec as a JSON file path or inline JSON string")
    ap.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    ap.add_argument("--limit", type=int, default=None, help="emit at most N records (preview)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"status": "error", "message": f"file not found: {path}"}))
        return 1

    # --detect (or a bare call with no mode): report structure, emit nothing.
    if args.detect or (not args.auto and not args.spec):
        print(json.dumps(detect(str(path), args.sheet), ensure_ascii=False, indent=2))
        return 0

    if args.spec:  # manual override wins
        spec_arg = args.spec.strip()
        spec = (
            json.loads(spec_arg)
            if spec_arg.startswith("{")
            else json.loads(Path(spec_arg).read_text(encoding="utf-8"))
        )
        records = unpivot(str(path), args.sheet, spec)
    else:  # --auto: infer then unpivot; echo the spec used to stderr for transparency
        spec, records = auto_unpivot(str(path), args.sheet)
        print(json.dumps({"spec_used": spec}, ensure_ascii=False), file=sys.stderr)
        if not records:
            print(
                "no records: sheet looks already tabular or no cross-tab axes found - "
                "use read_merged.py instead.",
                file=sys.stderr,
            )

    _emit(records, args.format, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
