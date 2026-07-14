#!/usr/bin/env python3
"""
docx_to_md.py

Converts a course-material .docx (typically a teaching syllabus / 教学大纲) into a
faithful, structure-preserving Markdown transcription, so the
course-knowledge-extraction workflow can then mine it.

This is the INPUT-side companion to md_to_docx.py (which handles the OUTPUT
side, md -> docx). It deliberately does NOT extract knowledge points, summarize,
or reorganize -- it transcribes. The goal is a clean intermediate `.md` that
preserves every section and, crucially, the syllabus's tables (course-info
table, the 教学内容/学时分配 table with chapters/hours/重点·难点, the 课程目标
table, 参考材料 list) as pipe tables, because those structures are exactly what
the extraction step reads. Semantic work (IDs, 知识域/类型, ratings, relationships)
happens afterwards, on this file -- not here.

Engines
-------
- pandoc (preferred): `pandoc -f docx -t gfm --wrap=none`. Robust, language-
  agnostic, turns docx tables into GFM pipe tables and paragraph styles into
  headings. This is the default when pandoc is on PATH.
- python-docx (fallback): walks the document body IN ORDER (paragraphs and
  tables interleaved, which python-docx's separate .paragraphs/.tables lists
  lose) and emits headings from paragraph style names, pipe tables from
  w:tbl elements, `**bold**` runs, and plain paragraphs. Used when pandoc is
  absent, or when forced with --engine python-docx.

`--engine auto` (default) tries pandoc, then falls back to python-docx.

Usage
-----
    python docx_to_md.py <input.docx> <output.md> \
        [--engine auto|pandoc|python-docx] [--keep-images] [--title "..."]

--keep-images   keep image references (default: drop standalone image lines,
                since syllabus figures rarely matter to extraction and clutter
                the transcription).
--title         prepend a top-level `# <title>` if the document has no leading
                heading (handy when the .docx starts straight into a table).

Requires pandoc on PATH for the default engine; the fallback requires the
`python-docx` package (`uv add python-docx`, or `pip install python-docx`
if running outside uv). At least one of the two must be available.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Shared post-processing
# --------------------------------------------------------------------------- #

IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
BLANK_RUN_RE = re.compile(r"\n{3,}")


def postprocess(md_text, keep_images=False):
    """Normalize whitespace, optionally drop standalone image lines, and make
    sure the file ends with exactly one newline. Kept deliberately light -- it
    tidies, it does not rewrite content."""
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in lines:
        if not keep_images and IMAGE_LINE_RE.match(line):
            continue
        out.append(line.rstrip())
    text = "\n".join(out)
    text = BLANK_RUN_RE.sub("\n\n", text)  # collapse 3+ blank lines -> 1 blank
    return text.strip("\n") + "\n"


def ensure_title(md_text, title):
    """Prepend `# <title>` unless the document already opens with an H1 that
    carries it. Used to restore a course title that pandoc promoted to metadata
    and dropped, or one the caller supplied explicitly via --title."""
    if not title:
        return md_text
    title = title.strip()
    first = next((ln for ln in md_text.splitlines() if ln.strip()), "")
    # Already have it as the leading H1? (e.g. python-docx kept the Title style.)
    if first.startswith("# ") and title in first:
        return md_text
    return f"# {title}\n\n{md_text}"


def summarize(md_text):
    """A quick, printable sanity summary of what came out."""
    headings = len(re.findall(r"^#{1,6}\s", md_text, flags=re.MULTILINE))
    table_seps = len(re.findall(r"^\s*\|?[\s:|-]+\|?\s*$", md_text, flags=re.MULTILINE))
    # a pipe table has exactly one separator row, so table_seps ~= table count
    return f"{headings} heading(s), ~{table_seps} table(s), {len(md_text.splitlines())} lines"


# --------------------------------------------------------------------------- #
# Engine 1: pandoc
# --------------------------------------------------------------------------- #


def convert_with_pandoc(input_path):
    """Return Markdown via pandoc, or None if pandoc isn't on PATH."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv (no shell=True); our own paths
            [pandoc, "-f", "docx", "-t", "gfm", "--wrap=none", str(input_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"pandoc failed: {stderr}") from e
    return result.stdout.decode("utf-8", "replace")


def pandoc_meta_title(input_path):
    """Recover a Word Title-styled course name that pandoc promotes to document
    metadata (and GFM output then drops). Reads it from the YAML block of a
    standalone conversion. Returns the title string, or None."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    try:
        out = subprocess.run(  # noqa: S603 -- fixed argv (no shell=True); our own paths
            [pandoc, "-f", "docx", "-t", "markdown", "-s", "--wrap=none", str(input_path)],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8", "replace")
    except subprocess.CalledProcessError:
        return None
    # YAML metadata block is delimited by leading/trailing --- lines.
    if not out.startswith("---"):
        return None
    for line in out.splitlines()[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"title:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


# --------------------------------------------------------------------------- #
# Engine 2: python-docx fallback (in-order body walk)
# --------------------------------------------------------------------------- #


def _runs_to_md(paragraph):
    """Render a paragraph's runs to inline Markdown, wrapping bold spans in
    **...**. Consecutive bold runs are merged so we don't emit ****."""
    parts = []
    bold_open = False
    for run in paragraph.runs:
        text = run.text
        if not text:
            continue
        if run.bold and not bold_open:
            parts.append("**")
            bold_open = True
        elif not run.bold and bold_open:
            parts.append("**")
            bold_open = False
        parts.append(text)
    if bold_open:
        parts.append("**")
    return "".join(parts).strip()


def _heading_level(style_name):
    """Map a Word paragraph style name to a Markdown heading level (or None)."""
    if not style_name:
        return None
    name = style_name.strip().lower()
    if name in ("title",):
        return 1
    m = re.match(r"heading\s*(\d+)", name)
    if m:
        return min(int(m.group(1)), 6)
    # Chinese Word builds sometimes localize style names.
    m = re.match(r"标题\s*(\d+)", style_name.strip())
    if m:
        return min(int(m.group(1)), 6)
    return None


def _cell_text(cell):
    """Flatten a table cell to single-line Markdown-safe text (escape pipes,
    join multi-paragraph cells with <br>)."""
    paras = [_runs_to_md(p) or p.text.strip() for p in cell.paragraphs]
    paras = [p for p in paras if p]
    text = "<br>".join(paras)
    return text.replace("|", "\\|")


def _table_to_md(table):
    """Render a docx table as a GFM pipe table. Merged cells: python-docx
    repeats the underlying cell object across the span, so a horizontally or
    vertically merged value simply repeats -- acceptable and readable for
    extraction, and avoids dropping content."""
    rows = table.rows
    if not rows:
        return ""
    n_cols = len(rows[0].cells)
    lines = []
    header = [_cell_text(c) for c in rows[0].cells]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for row in rows[1:]:
        cells = [_cell_text(c) for c in row.cells]
        # pad/truncate to header width so the pipe table stays rectangular
        cells = (cells + [""] * n_cols)[:n_cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def convert_with_python_docx(input_path):
    """Return Markdown by walking the docx body in document order."""
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        return None

    doc = Document(str(input_path))
    body = doc.element.body
    blocks = []
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = _runs_to_md(para)
            if not text:
                continue
            level = _heading_level(para.style.name if para.style else None)
            if level:
                blocks.append("#" * level + " " + text)
            elif para.style and "list" in (para.style.name or "").lower():
                blocks.append("- " + text)
            else:
                blocks.append(text)
        elif tag == "tbl":
            table = Table(child, doc)
            md = _table_to_md(table)
            if md:
                blocks.append(md)
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def convert(input_path, engine="auto", keep_images=False, title=None):
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    md = None
    used = None
    effective_title = title  # explicit --title wins

    if engine in ("auto", "pandoc"):
        md = convert_with_pandoc(input_path)
        if md is not None:
            used = "pandoc"
            # pandoc drops a Word Title-styled course name; recover it unless the
            # caller already supplied one.
            if not effective_title:
                effective_title = pandoc_meta_title(input_path)
        elif engine == "pandoc":
            raise RuntimeError("pandoc not found on PATH (engine forced to pandoc).")

    if md is None and engine in ("auto", "python-docx"):
        md = convert_with_python_docx(input_path)
        if md is not None:
            used = "python-docx"  # this path already keeps the Title style as an H1
        elif engine == "python-docx":
            raise RuntimeError("python-docx not installed (engine forced to python-docx).")

    if md is None:
        raise RuntimeError(
            "No conversion engine available: install pandoc (preferred) or the python-docx package."
        )

    md = postprocess(md, keep_images=keep_images)
    md = ensure_title(md, effective_title)
    md = postprocess(md, keep_images=True)  # final whitespace normalize only
    return md, used


def main():
    parser = argparse.ArgumentParser(
        description="Convert a course-material .docx into a faithful Markdown transcription."
    )
    parser.add_argument("input", help="Path to input .docx file")
    parser.add_argument("output", help="Path to output .md file")
    parser.add_argument(
        "--engine",
        choices=("auto", "pandoc", "python-docx"),
        default="auto",
        help="Conversion engine (default: auto = pandoc, then python-docx fallback).",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep image references (default: drop standalone image lines).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Prepend a `# <title>` heading if the document has no leading heading.",
    )
    args = parser.parse_args()

    try:
        md, used = convert(
            args.input, engine=args.engine, keep_images=args.keep_images, title=args.title
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"Converted {args.input} -> {args.output} (engine: {used})")
    print(f"  {summarize(md)}")


if __name__ == "__main__":
    main()
