#!/usr/bin/env python3
"""Parse a knowledge-point deep-dive (Output 3, ``03_[课程名]_知识点详解``) back into
structured records — the ingestion engine for lesson-plan (教案) generation.

The teacher-corrected deliverables exist only as ``.docx``, and the machine-readable
knowledge base (Output 2) is absent, so this script treats **Output 3 as the sole
source of truth**. Give it a ``.docx`` (converted to Markdown via ``docx_to_md.py``
first) or an already-converted ``.md``; it emits one JSON document with **English keys
only** and no redundant fields (the course name / English name / total hours live once,
inside ``profile``)::

    {
      "course":  {overview,
                  profile{course_name, english_name, college, major, audience, total_hours},
                  objectives_intro, objectives[{seq, text, supports}]},
      "points":  [{id, name, knowledge_domain, point_type, importance, difficulty,
                   supports_objectives[int], definition, learning_significance,
                   detailed_explanation, examples, key_points_and_difficulties,
                   prerequisites[id], successors[id], related[id]}]
    }

Every per-point prose field is optional (a point may carry only 定义 + 详细讲解), so a
missing section yields ``""``/``[]`` rather than an error. ``learning_outcomes`` is
deliberately *not* produced here — it lived in the (absent) knowledge base and is
synthesised downstream by the lesson-plan step.

Usage::

    python parse_knowledge.py 03_海洋管理概论_知识点详解.docx -o points.json --pretty
    python parse_knowledge.py 03_xxx.md --select K001,K002,K008
"""

# This file parses Chinese source documents, so fullwidth punctuation appears in
# string literals and comments by design — silence the ambiguous-character
# lint (RUF001 in strings, RUF003 in comments).
# ruff: noqa: RUF001, RUF003
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- knowledge-point heading, e.g. "### K001 海洋管理的概念" (any heading level) ---
KPOINT_RE = re.compile(r"^#{1,6}\s*(K\d+)\s+(.*\S)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# a whole-line bold label marking a field section, e.g. "**详细讲解**"
FIELD_RE = re.compile(r"^\*\*\s*(.+?)\s*\*\*\s*$")
KID_RE = re.compile(r"K\d+")
STAR = "★"

# 课程概况 table rows use Chinese labels; map them to the fixed English profile keys.
# The profile is a fixed-shape object of exactly these fields (in this order); any
# unrecognised row is dropped so no stray/duplicate keys leak into the JSON.
PROFILE_FIELDS = ("course_name", "english_name", "college", "major", "audience", "total_hours")
PROFILE_KEY_MAP = {
    "课程名称": "course_name",
    "英文名称": "english_name",
    "开课学院": "college",
    "开设学院": "college",  # synonym variant seen in some syllabi
    "适用专业": "major",
    "授课对象": "audience",
    "总学时": "total_hours",
}

# the controlled vocabularies from SKILL.md (used only by --lint to flag drift)
VALID_DOMAINS = {
    "基础理论",
    "核心概念",
    "基本原理",
    "制度体系",
    "方法工具",
    "应用实践",
    "热点问题",
    "中国实践",
    "前沿发展",
}
VALID_TYPES = {
    "概念",
    "理论",
    "原理",
    "原则",
    "制度",
    "法律文件",
    "国际组织",
    "治理机制",
    "方法",
    "模型",
    "案例",
    "热点问题",
    "中国实践",
    "专业术语",
}


@dataclass
class KnowledgePoint:
    # --- identity (from the "### K001 名称" heading) ---
    id: str  # 知识点唯一编号，如 "K001"；选点/排序/关联的键
    name: str  # 知识点名称
    # --- attributes (from the "域 · 类型 · 重要程度 · 难度 · 支撑课程目标" meta line) ---
    knowledge_domain: str = ""  # 9 个知识域之一（基础理论/核心概念/…/前沿发展），每点恰好一个
    point_type: str = ""  # 14 个知识点类型之一（概念/理论/原理/…/专业术语）
    importance: int = 3  # 重要程度 1–5，由 ★ 数换算（★★★★★→5）；默认 3=★★★☆☆
    difficulty: int = 3  # 难度 1–5，由 ★ 数换算；默认 3=★★★☆☆
    supports_objectives: list[int] = field(default_factory=list)  # 支撑的课程目标编号，如 [1, 4]
    # --- content (prose sections; may be empty if the source omits a section) ---
    definition: str = ""  # 「定义」段：知识点的定义
    learning_significance: str = ""  # 「为什么重要 / 学习意义」段：为什么学它
    detailed_explanation: str = ""  # 「详细讲解」段：多段落实质讲解，03 的核心内容
    examples: str = ""  # 「示例 / 案例」段：具体案例
    key_points_and_difficulties: str = ""  # 「重点与难点提示」段：学生易错/需关注处
    # --- relations (from the "关联" bullets: 前置 / 后续 / 关联) ---
    # 每个都是 {知识点 ID: 规范名}，名字由第二趟按 ID 回查本课其它点的 name 得到（非关联行里的简称）
    prerequisites: dict[str, str] = field(default_factory=dict)  # 前置知识点，排授课顺序用
    successors: dict[str, str] = field(default_factory=dict)  # 后续知识点
    related: dict[str, str] = field(default_factory=dict)  # 关联（非前置/后续）知识点


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _strip_cell(text: str) -> str:
    """Cell text without surrounding pipes-remnants or bold markers."""
    return text.strip().strip("*").strip()


def _is_table_sep(line: str) -> bool:
    return bool(re.fullmatch(r"\|[\s\-:|]+\|?", line.strip()))


def _table_rows(block: list[str]) -> list[list[str]]:
    """Return data rows (header + separator dropped) of the first pipe-table in *block*."""
    rows: list[list[str]] = []
    seen_sep = False
    for ln in block:
        s = ln.strip()
        if not s.startswith("|"):
            if rows:  # table ended
                break
            continue
        if _is_table_sep(s):
            seen_sep = True
            continue
        cells = [_strip_cell(c) for c in s.strip("|").split("|")]
        if not seen_sep:  # header row — skip once separator seen; keep if no sep at all
            header_pending = cells
            rows.append(header_pending)
            continue
        rows.append(cells)
    # drop the header row we optimistically appended if a separator confirmed it
    if seen_sep and rows:
        return rows[1:]
    return rows


def _paragraph_before_table(block: list[str]) -> str:
    out: list[str] = []
    for ln in block:
        if ln.strip().startswith("|"):
            break
        if ln.strip():
            out.append(ln.strip())
    return "\n".join(out).strip()


def _count_stars(text: str, default: int = 3) -> int:
    n = text.count(STAR)
    return n if 1 <= n <= 5 else default


# ---------------------------------------------------------------------------
# section parsers
# ---------------------------------------------------------------------------
def split_title(text: str) -> tuple[str, str]:
    """ "海洋管理概论 / Introduction to Marine Management 知识点详解" -> (中文名, 英文名)."""
    t = re.sub(r"\s*知识点详解\s*$", "", text).strip()
    if " / " in t:
        zh, en = t.split(" / ", 1)
        return zh.strip(), en.strip()
    return t, ""


def parse_course_profile(block: list[str]) -> tuple[str, dict[str, str]]:
    """Return (overview, profile) with profile keyed by the fixed English schema names.

    The source 课程概况 table uses Chinese labels; translate them via PROFILE_KEY_MAP
    into a fixed-shape object of PROFILE_FIELDS (each defaulting to ``""``), dropping any
    unrecognised row so the profile stays clean and duplicate-free.
    """
    overview = _paragraph_before_table(block)
    profile = dict.fromkeys(PROFILE_FIELDS, "")
    for r in _table_rows(block):
        if len(r) < 2:
            continue
        key = PROFILE_KEY_MAP.get(r[0].strip())
        if key:
            profile[key] = r[1]
    return overview, profile


def parse_objectives(block: list[str]) -> tuple[str, list[dict]]:
    intro = _paragraph_before_table(block)
    objectives: list[dict] = []
    for r in _table_rows(block):
        if len(r) < 2:
            continue
        seq_raw = r[0]
        m = re.search(r"\d+", seq_raw)
        objectives.append(
            {
                "seq": int(m.group()) if m else None,
                "text": r[1],
                "supports": r[2] if len(r) >= 3 else "",
            }
        )
    return intro, objectives


def _parse_meta(line: str, point: KnowledgePoint) -> None:
    """Parse "基础理论 · 理论 · 重要程度 ★★★★★ · 难度 ★★★☆☆ · 支撑课程目标 1、4"."""
    parts = [p for p in re.split(r"\s*·\s*", line.strip().strip("*").strip()) if p]
    positional: list[str] = []
    for p in parts:
        if "重要程度" in p:
            point.importance = _count_stars(p)
        elif "难度" in p:
            point.difficulty = _count_stars(p)
        elif "支撑课程目标" in p or "课程目标" in p:
            point.supports_objectives = [int(x) for x in re.findall(r"\d+", p)]
        else:
            positional.append(p)
    if positional:
        point.knowledge_domain = positional[0]
    if len(positional) >= 2:
        point.point_type = positional[1]


# field-label (normalised, spaces removed) -> KnowledgePoint attribute
def _field_key(label: str) -> str | None:
    norm = label.replace(" ", "")
    if "定义" in norm:
        return "definition"
    if "学习意义" in norm or "为什么重要" in norm:
        return "learning_significance"
    if "详细讲解" in norm:
        return "detailed_explanation"
    if "示例" in norm or "案例" in norm:
        return "examples"
    if "重点" in norm and "难点" in norm:
        return "key_points_and_difficulties"
    if "关联" in norm:
        return "relations"
    return None


def _parse_relations(body: str, point: KnowledgePoint) -> None:
    """Parse 关联 bullets (前置/后续/关联) into {id: ""} dicts; names filled in pass 2.

    Only the K-ids are captured here — the name written inline in the bullet is often
    an abbreviation of the target point's real name, so it is discarded and the
    canonical name is resolved later against the full point set (`_resolve_relation_names`).
    """
    for raw in body.splitlines():
        s = raw.strip().lstrip("-").strip()
        if not s or ("：" not in s and ":" not in s):
            continue
        key, val = re.split(r"[:：]", s, maxsplit=1)
        key = key.strip()
        if "前置" in key:
            target = point.prerequisites
        elif "后续" in key:
            target = point.successors
        else:  # 关联 / other
            target = point.related
        for i in KID_RE.findall(val):
            target.setdefault(i, "")


def parse_point(pid: str, name: str, domain: str, block: list[str]) -> KnowledgePoint:
    point = KnowledgePoint(id=pid, name=name, knowledge_domain=domain or "")
    # locate the meta line (first non-empty, non-field line) and field sections
    fields: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    meta_done = False
    for ln in block:
        fm = FIELD_RE.match(ln.strip())
        if fm:
            current = []
            fields.append((fm.group(1), current))
            meta_done = True
            continue
        if not meta_done:
            if (ln.strip() and STAR in ln) or ("·" in ln and ln.strip()):
                _parse_meta(ln, point)
                meta_done = True
            continue
        if current is not None:
            current.append(ln)

    for label, body_lines in fields:
        key = _field_key(label)
        if key is None:
            continue
        body = "\n".join(body_lines).strip()
        if key == "relations":
            _parse_relations(body, point)
        else:
            setattr(point, key, body)
    return point


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------
def _collect_until_heading(lines: list[str], start: int) -> tuple[list[str], int]:
    block: list[str] = []
    i = start
    while i < len(lines):
        if HEADING_RE.match(lines[i]):
            break
        block.append(lines[i])
        i += 1
    return block, i


def parse_markdown(md_text: str) -> dict:
    lines = md_text.splitlines()
    course: dict = {
        "overview": "",
        "profile": dict.fromkeys(PROFILE_FIELDS, ""),
        "objectives_intro": "",
        "objectives": [],
    }
    # H1 title is captured only as a fallback for the profile name fields (below),
    # not stored on its own — course_name/english_name are the single source of truth.
    title_zh = title_en = ""
    points: list[KnowledgePoint] = []
    current_domain = ""
    i, n = 0, len(lines)
    while i < n:
        h = HEADING_RE.match(lines[i])
        if not h:
            i += 1
            continue
        level, text = len(h.group(1)), h.group(2).strip()
        kp = KPOINT_RE.match(lines[i])
        if kp:
            block, i = _collect_until_heading(lines, i + 1)
            points.append(parse_point(kp.group(1), kp.group(2).strip(), current_domain, block))
            continue
        if level == 1:
            title_zh, title_en = split_title(text)
        elif text.startswith("课程概况"):
            block, i = _collect_until_heading(lines, i + 1)
            course["overview"], course["profile"] = parse_course_profile(block)
            continue
        elif text.startswith("课程目标"):
            block, i = _collect_until_heading(lines, i + 1)
            course["objectives_intro"], course["objectives"] = parse_objectives(block)
            continue
        elif text.startswith("知识域"):
            current_domain = text.split("：", 1)[1].strip() if "：" in text else text
        i += 1
    # Fall back to the H1 title when the 课程概况 table omits the name fields, so
    # dropping the old top-level title/title_en is lossless.
    if not course["profile"]["course_name"]:
        course["profile"]["course_name"] = title_zh
    if not course["profile"]["english_name"]:
        course["profile"]["english_name"] = title_en
    _resolve_relation_names(points)
    return {"course": course, "points": [asdict(p) for p in points]}


def _resolve_relation_names(points: list[KnowledgePoint]) -> None:
    """Pass 2: fill each relation dict's value with the target point's canonical name.

    A dangling reference (id not among the parsed points) keeps an empty name — the
    key is preserved so ``--lint`` can still flag it.
    """
    name_map = {p.id: p.name for p in points}
    for p in points:
        for rel in (p.prerequisites, p.successors, p.related):
            for kid in rel:
                rel[kid] = name_map.get(kid, "")


def load(path: str | Path, engine: str = "auto") -> dict:
    """Parse from a ``.docx`` (converted first) or ``.md`` file into the JSON structure."""
    path = Path(path)
    if path.suffix.lower() == ".docx":
        sys.path.insert(0, str(Path(__file__).parent))
        from docx_to_md import convert  # local sibling script

        md_text, _ = convert(path, engine=engine)
    else:
        md_text = path.read_text(encoding="utf-8")
    return parse_markdown(md_text)


# ---------------------------------------------------------------------------
# lint — surface teacher-editing inconsistencies in the source deep-dive
# ---------------------------------------------------------------------------
def lint_document(data: dict) -> list[tuple[str, str, str]]:
    """Return findings as (severity, point_id, message). severity ∈ ERROR/WARN/INFO."""
    findings: list[tuple[str, str, str]] = []
    points = data["points"]
    ids = [p["id"] for p in points]
    id_set = set(ids)
    obj_seqs = {o["seq"] for o in data["course"]["objectives"] if o["seq"] is not None}

    # duplicate IDs (same K-number reused for different/identical points)
    for pid in sorted({i for i in ids if ids.count(i) > 1}):
        findings.append(("ERROR", pid, f"重复 ID：出现 {ids.count(pid)} 次"))

    for p in points:
        pid = p["id"]
        if p["knowledge_domain"] not in VALID_DOMAINS:
            findings.append(("ERROR", pid, f"非法/复合知识域：{p['knowledge_domain']!r}"))
        if p["point_type"] not in VALID_TYPES:
            findings.append(("ERROR", pid, f"非法知识点类型：{p['point_type']!r}"))
        if not p["definition"].strip():
            findings.append(("ERROR", pid, "缺少「定义」"))
        if not p["detailed_explanation"].strip():
            findings.append(("WARN", pid, "缺少「详细讲解」"))
        # relations pointing at a K-id absent from this document
        for kind in ("prerequisites", "successors", "related"):
            for ref in p[kind]:
                if ref not in id_set:
                    findings.append(("WARN", pid, f"{kind} 指向不存在的 {ref}"))
        # supports a course objective that isn't defined
        for seq in p["supports_objectives"]:
            if obj_seqs and seq not in obj_seqs:
                findings.append(
                    ("WARN", pid, f"支撑课程目标 {seq}，但课程只定义了 {sorted(obj_seqs)}")
                )

    # ID gaps (informational — teacher may have intentionally dropped a point)
    nums = sorted(int(i[1:]) for i in id_set if i[1:].isdigit())
    if nums:
        missing = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
        if missing:
            gaps = ", ".join(f"K{n:03d}" for n in missing)
            findings.append(("INFO", "-", f"ID 不连续，缺号：{gaps}"))
    return findings


def discover_docs(inputs: list[Path]) -> list[Path]:
    """Expand file/dir inputs to a list of 03 deep-dive docs, skipping hidden dirs."""
    docs: list[Path] = []
    for p in inputs:
        if p.is_dir():
            found = list(p.glob("**/03_*_知识点详解.docx")) + list(p.glob("**/03_*_知识点详解.md"))
            # skip hidden dirs (e.g. .backup_precorrection) so backups aren't picked up
            docs += sorted(d for d in found if not any(part.startswith(".") for part in d.parts))
        else:
            docs.append(p)
    return docs


def run_batch(inputs: list[Path], engine: str, out_dir: Path | None) -> int:
    """Export one <课程名>_knowledge.json per course. Skips docs with lint ERRORs."""
    docs = discover_docs(inputs)
    if not docs:
        print("no 03_*_知识点详解 files found", file=sys.stderr)
        return 2

    written = skipped = 0
    for d in docs:
        data = load(d, engine=engine)
        course = d.parent.name  # course-folder name (stable, no English suffix)
        errs = [f for f in lint_document(data) if f[0] == "ERROR"]
        if errs:
            skipped += 1
            print(f"✗ {course}: {len(errs)} ERROR，跳过导出（先修正源文档）", file=sys.stderr)
            for sev, pid, msg in errs:
                print(f"    [{sev}] {pid} {msg}", file=sys.stderr)
            continue
        dest = (
            (out_dir / f"{course}_knowledge.json")
            if out_dir
            else d.parent / f"{course}_knowledge.json"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
        print(f"✓ {course}: {len(data['points'])} 知识点 -> {dest}")
    print(f"\n=== 导出 {written} 份，跳过 {skipped} 份 ===")
    return 1 if skipped else 0


def run_lint(inputs: list[Path], engine: str) -> int:
    docs = discover_docs(inputs)
    if not docs:
        print("no 03_*_知识点详解 files found", file=sys.stderr)
        return 2

    total_err = total_warn = 0
    for d in docs:
        data = load(d, engine=engine)
        title = data["course"]["profile"]["course_name"] or d.stem
        findings = lint_document(data)
        errs = sum(1 for f in findings if f[0] == "ERROR")
        warns = sum(1 for f in findings if f[0] == "WARN")
        total_err += errs
        total_warn += warns
        flag = "✗" if errs else ("!" if warns else "✓")
        print(f"\n{flag} {title}  ({len(data['points'])} 知识点)")
        if not findings:
            print("    无问题")
        for sev, pid, msg in findings:
            print(f"    [{sev:5s}] {pid:>5s}  {msg}")
    print(f"\n=== 共 {len(docs)} 份文档：{total_err} ERROR / {total_warn} WARN ===")
    return 1 if total_err else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "input", nargs="+", help="03_..._知识点详解 file(s) or a directory (with --lint)"
    )
    ap.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    ap.add_argument("--engine", choices=["auto", "pandoc", "python-docx"], default="auto")
    ap.add_argument("--select", help="comma-separated point IDs to keep, e.g. K001,K008")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON output")
    ap.add_argument(
        "--lint", action="store_true", help="report source inconsistencies instead of JSON"
    )
    ap.add_argument(
        "--batch",
        action="store_true",
        help="export one <课程名>_knowledge.json per course (file(s) or a directory)",
    )
    ap.add_argument(
        "--out-dir", help="with --batch: write all JSONs here instead of beside each docx"
    )
    args = ap.parse_args()

    if args.lint:
        return run_lint([Path(p) for p in args.input], args.engine)

    if args.batch:
        out_dir = Path(args.out_dir) if args.out_dir else None
        return run_batch([Path(p) for p in args.input], args.engine, out_dir)

    if len(args.input) != 1:
        ap.error("JSON mode takes exactly one input (use --batch/--lint for many/a directory)")

    data = load(args.input[0], engine=args.engine)
    if args.select:
        wanted = {s.strip() for s in args.select.split(",") if s.strip()}
        data["points"] = [p for p in data["points"] if p["id"] in wanted]

    text = json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        pts = len(data["points"])
        print(f"Parsed {pts} knowledge point(s) -> {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
