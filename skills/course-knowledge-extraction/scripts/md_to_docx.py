#!/usr/bin/env python3
"""
md_to_docx.py

Converts a knowledge-map markdown file (as produced by the
course-knowledge-extraction skill) into a formatted .docx.

Handles the fixed subset of Markdown this skill's output actually uses:
  - Headings: #, ##, ### (#### also supported)
  - Pipe tables: | a | b | ... | with a |---|---| separator row
  - Bullet lists: lines starting with "- " or "* "
  - Numbered lists: lines starting with "1. "
  - Fenced code blocks: ``` ... ``` (rendered as monospace, e.g. the
    tree-style knowledge map diagram)
  - Inline **bold**
  - Plain paragraphs

This is NOT a general CommonMark parser -- it's intentionally narrow,
tuned to this skill's own output shape, so conversion stays predictable.

Typography: uses a dual-font setup by default -- a Latin font (Calibri)
for ASCII characters/numbers and a CJK font (Microsoft YaHei) for Chinese
characters, since forcing a single CJK font onto everything tends to make
numbers and English text look worse than necessary.

Wide tables (6+ columns, e.g. the knowledge-point overview table) get a
smaller font and tighter row spacing automatically so they stay readable
without wrapping every cell into a wall of text.

Optionally also exports a PDF (via LibreOffice/soffice) and PNG page previews
(via pdftoppm) so one call can produce docx + pdf + a visual spot-check.

Usage:
    python md_to_docx.py <input.md> <output.docx> \
        [--cjk-font "Microsoft YaHei"] [--latin-font "Calibri"] \
        [--pdf] [--preview N] [--dpi 100]

Requires the `python-docx` package (`uv add python-docx`, or `pip install python-docx`
if running outside uv). --pdf needs `soffice`/`libreoffice` on PATH; --preview also
needs `pdftoppm` (poppler). Both degrade gracefully with a note if the tool is absent.
"""

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
NUMBERED_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
ITALIC_RE = re.compile(r"^\*([^*].*?)\*$")  # single-* note line; won't swallow a **bold** line

WIDE_TABLE_THRESHOLD = 6  # columns; tables at/above this get compacted styling


def set_run_font(run, cjk_font, latin_font=None):
    """Set fonts so Latin characters/numbers use latin_font while CJK characters
    use cjk_font. If latin_font is None, use cjk_font for both (single-font mode)."""
    ascii_font = latin_font or cjk_font
    run.font.name = ascii_font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), cjk_font)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)


def tighten_paragraph_spacing(paragraph, space_pt=1):
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(space_pt)
    fmt.space_after = Pt(space_pt)


def add_runs_with_bold(paragraph, text, cjk_font, latin_font=None, italic=False, size=None):
    """Add text to a paragraph, splitting on **bold** spans."""
    last_index = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > last_index:
            run = paragraph.add_run(text[last_index : m.start()])
            set_run_font(run, cjk_font, latin_font)
            run.italic = italic
            if size:
                run.font.size = size
        run = paragraph.add_run(m.group(1))
        set_run_font(run, cjk_font, latin_font)
        run.bold = True
        run.italic = italic
        if size:
            run.font.size = size
        last_index = m.end()
    if last_index < len(text):
        run = paragraph.add_run(text[last_index:])
        set_run_font(run, cjk_font, latin_font)
        run.italic = italic
        if size:
            run.font.size = size


def is_table_separator(line):
    stripped = line.strip()
    return bool(re.match(r"^\|?[\s:|-]+\|?$", stripped)) and "-" in stripped


def split_table_row(line):
    cells = line.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [c.strip() for c in cells.split("|")]


def shade_cell(cell, fill_hex="D9E2F3"):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.makeelement(
        qn("w:shd"),
        {
            qn("w:val"): "clear",
            qn("w:color"): "auto",
            qn("w:fill"): fill_hex,
        },
    )
    tc_pr.append(shd)


def display_width(text):
    """Approximate rendered width in "Latin-char units": CJK glyphs occupy ~2x the
    width of an ASCII character, so count them as 2. Keeps Chinese-heavy columns
    (知识点, 一句话说明) from being under-allocated relative to ID/star columns."""
    width = 0
    for ch in str(text):
        # CJK ranges (approx): common ideographs, compatibility, fullwidth forms.
        width += 2 if ord(ch) > 0x2E7F else 1
    return width


def compute_column_widths(rows, n_cols, total_cm=17.0, min_cm=1.4):
    """Heuristic proportional column widths based on max content width per column,
    with a floor so narrow columns (IDs, star ratings, objective numbers) don't
    get squeezed to nothing."""
    max_lens = [1] * n_cols
    for row in rows:
        for c_idx, cell in enumerate(row):
            max_lens[c_idx] = max(max_lens[c_idx], display_width(cell))
    total_len = sum(max_lens)
    raw_widths = [max(min_cm, total_cm * (wlen / total_len)) for wlen in max_lens]
    scale = total_cm / sum(raw_widths)
    return [w * scale for w in raw_widths]


def build_table(doc, rows, cjk_font, latin_font=None):
    n_cols = len(rows[0])
    is_wide = n_cols >= WIDE_TABLE_THRESHOLD
    font_size = Pt(8 if is_wide else 10)
    header_size = Pt(8 if is_wide else 10)

    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"

    col_widths = compute_column_widths(rows, n_cols) if is_wide else None

    # Fixed layout is required for per-cell widths to actually stick — with the
    # default autofit, Word/LibreOffice recompute column widths from content and
    # ignore the Cm(...) values set below.
    if col_widths:
        table.autofit = False
        tbl_pr = table._tbl.tblPr
        layout = tbl_pr.makeelement(qn("w:tblLayout"), {qn("w:type"): "fixed"})
        tbl_pr.append(layout)

    for r_idx, row_cells in enumerate(rows):
        for c_idx, cell_text in enumerate(row_cells):
            cell = table.cell(r_idx, c_idx)
            cell.paragraphs[0].text = ""
            p = cell.paragraphs[0]
            size = header_size if r_idx == 0 else font_size
            add_runs_with_bold(p, cell_text, cjk_font, latin_font, size=size)
            if is_wide:
                tighten_paragraph_spacing(p, space_pt=1)
            if r_idx == 0:
                for run in p.runs:
                    run.bold = True
                shade_cell(cell)
            if col_widths:
                cell.width = Cm(col_widths[c_idx])
    if col_widths:
        for row in table.rows:
            for c_idx, w in enumerate(col_widths):
                row.cells[c_idx].width = Cm(w)
    return table


def convert(md_text, cjk_font, latin_font=None):
    lines = md_text.split("\n")
    doc = Document()

    # base style font
    style = doc.styles["Normal"]
    style.font.name = latin_font or cjk_font  # type: ignore
    style.font.size = Pt(10.5)  # type: ignore
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), cjk_font)
    rfonts.set(qn("w:ascii"), latin_font or cjk_font)
    rfonts.set(qn("w:hAnsi"), latin_font or cjk_font)

    # A4/Letter-friendly margins (2.54cm all round, matches Word defaults)
    section = doc.sections[0]
    section.page_width = Cm(21.59)
    section.page_height = Cm(27.94)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)

    first_heading_used = False
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Fenced code block
        if line.strip().startswith("```"):
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            for cl in code_lines:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run(cl if cl else " ")
                set_run_font(run, "Consolas", "Consolas")
                run.font.size = Pt(9)
            doc.add_paragraph("")
            continue

        # Heading
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if not first_heading_used and level == 1:
                p = doc.add_heading(level=0)
                add_runs_with_bold(p, text, cjk_font, latin_font)
                first_heading_used = True
            else:
                heading_level = min(level, 3)
                p = doc.add_heading(level=heading_level)
                add_runs_with_bold(p, text, cjk_font, latin_font)
            i += 1
            continue

        # Table
        if line.strip().startswith("|") and i + 1 < n and is_table_separator(lines[i + 1]):
            table_rows = [split_table_row(line)]
            i += 2  # skip header + separator
            while i < n and lines[i].strip().startswith("|"):
                table_rows.append(split_table_row(lines[i]))
                i += 1
            build_table(doc, table_rows, cjk_font, latin_font)
            doc.add_paragraph("")
            continue

        # Bullet list
        m = BULLET_RE.match(line)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            add_runs_with_bold(p, m.group(1), cjk_font, latin_font)
            i += 1
            continue

        # Numbered list
        m = NUMBERED_RE.match(line)
        if m:
            p = doc.add_paragraph()
            add_runs_with_bold(p, f"{m.group(1)}. {m.group(2)}", cjk_font, latin_font)
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^\s*---+\s*$", line):
            doc.add_paragraph("")
            i += 1
            continue

        # Blank line
        if line.strip() == "":
            i += 1
            continue

        # Italic-only line (used as a note)
        m = ITALIC_RE.match(line)
        if m:
            p = doc.add_paragraph()
            add_runs_with_bold(p, m.group(1), cjk_font, latin_font, italic=True)
            i += 1
            continue

        # Plain paragraph
        p = doc.add_paragraph()
        add_runs_with_bold(p, line, cjk_font, latin_font)
        i += 1

    return doc


def export_pdf(docx_path):
    """Convert a .docx to .pdf in the same directory via LibreOffice (soffice).

    Returns the PDF path, or None if soffice/libreoffice isn't on PATH (in which
    case a note is printed and the .docx is left as the only output). Each call
    uses a throwaway LibreOffice profile so parallel conversions don't collide on
    the shared profile lock."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print("PDF export skipped: soffice/libreoffice not found on PATH.")
        return None
    docx_path = Path(docx_path)
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        subprocess.run(  # noqa: S603 — fixed argv (no shell=True); inputs are our own paths
            [
                soffice,
                f"-env:UserInstallation=file://{profile}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(docx_path.parent),
                str(docx_path),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"PDF export failed: {e.stderr.decode('utf-8', 'replace').strip()}")
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    pdf_path = docx_path.with_suffix(".pdf")
    if pdf_path.exists():
        print(f"Exported PDF -> {pdf_path}")
        return pdf_path
    print("PDF export failed: soffice ran but produced no PDF.")
    return None


def render_previews(pdf_path, pages, dpi=100):
    """Render the first `pages` pages of a PDF to PNGs via pdftoppm, for a quick
    visual spot-check. Returns the list of PNG paths (empty if pdftoppm is
    unavailable or `pages` < 1). Files are named <pdf-stem>_preview-N.png."""
    if not pdf_path or pages < 1:
        return []
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        print("Preview skipped: pdftoppm not found on PATH.")
        return []
    pdf_path = Path(pdf_path)
    prefix = pdf_path.with_name(f"{pdf_path.stem}_preview")
    try:
        subprocess.run(  # noqa: S603 — fixed argv (no shell=True); inputs are our own paths
            [pdftoppm, "-png", "-r", str(dpi), "-l", str(pages), str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Preview failed: {e.stderr.decode('utf-8', 'replace').strip()}")
        return []
    pngs = sorted(pdf_path.parent.glob(f"{prefix.name}-*.png"))
    if pngs:
        print(f"Rendered {len(pngs)} preview page(s): {', '.join(p.name for p in pngs)}")
    return pngs


def main():
    parser = argparse.ArgumentParser(description="Convert a knowledge-map markdown file to .docx")
    parser.add_argument("input", help="Path to input .md file")
    parser.add_argument("output", help="Path to output .docx file")
    parser.add_argument(
        "--cjk-font",
        default="Microsoft YaHei",
        help="Font for Chinese/CJK characters (default: Microsoft YaHei)",
    )
    parser.add_argument(
        "--latin-font",
        default="Calibri",
        help="Font for Latin characters/numbers (default: Calibri). "
        "Pass the same value as --cjk-font for single-font mode.",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Also export a .pdf next to the .docx (via LibreOffice/soffice).",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=0,
        metavar="N",
        help="Render the first N pages of the PDF to PNG previews for a "
        "visual spot-check (implies --pdf). 0 = none (default).",
    )
    parser.add_argument(
        "--dpi", type=int, default=100, help="Resolution for --preview PNGs (default: 100)."
    )
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        md_text = f.read()

    doc = convert(md_text, args.cjk_font, args.latin_font)
    doc.save(args.output)
    print(
        f"Converted {args.input} -> {args.output} (cjk: {args.cjk_font}, latin: {args.latin_font})"
    )

    if args.pdf or args.preview:
        pdf_path = export_pdf(args.output)
        if args.preview:
            render_previews(pdf_path, args.preview, args.dpi)


if __name__ == "__main__":
    main()
