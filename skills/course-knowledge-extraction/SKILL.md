---
name: course-knowledge-extraction
description: "Use when the user provides course material — teaching syllabi (教学大纲), lecture notes, transcripts, articles — and wants it summarized, organized, or turned into study notes or a knowledge base. Triggers: 'course notes', 'lecture notes', 'study guide', 'syllabus', '教学大纲', '知识点', '知识库', 'knowledge map', 'summarize this course', 'extract key concepts', or any text-based educational content wanting a structured breakdown. Produces, for a syllabus, a student-facing knowledge map and a readable knowledge-point deep-dive (both with matching .docx) plus a machine-readable knowledge base; for lecture content, a concept-and-example study outline. Preserves source language unless translation is requested. Not for one-line document questions or quick answers."
---
# Course Knowledge Extraction

Turns raw, text-based course material (teaching syllabi, notes, transcripts, articles, readings) into structured knowledge outputs — for syllabi, a student-facing knowledge map, a readable knowledge-point deep-dive, and a machine-readable knowledge base; for lecture content, a concept-level study outline.

## Workflow

### 1. Get the source material

- If the user referenced a file, read it with your filesystem/read tools; if a path is relative, it resolves against the session workspace (your cwd).
- If the user pasted text directly, use that.
- **Input scope:** this skill handles text-based/Markdown input. If the user provides a PDF or DOCX syllabus, first extract it to text with a shell tool available in the sandbox (e.g. `pdftotext file.pdf -` for PDF, or `pandoc file.docx -t markdown` / `python-docx` for DOCX), then apply this workflow — don't attempt to parse binary formats directly here.
- If material is split across multiple files/sources, read all of them before extracting — don't summarize partial input.

### 2. Identify the material type, then extract accordingly

First figure out which kind of course material this is — the right output shape is different for each.

#### Type A — Teaching syllabus / curriculum document (教学大纲)

Recognize this type by structure like: course basic info (课程基本信息), course objectives (课程目标), a teaching-content table with chapters/hours/key-difficult points (教学内容、要求与学时分配), assessment method (课程考核评价方式), teaching methods (教学方法), reference materials (参考材料).

**Extraction principle:** transform the syllabus into structured knowledge — never copy it verbatim, never invent knowledge points not implied by the source, and keep terminology/naming consistent across every knowledge point.

Mine sections in this priority order, each feeding a different aspect of the knowledge points:

1. **课程目标 (Course Objectives)** → map each objective into one or more measurable learning outcomes (see OBE rule below); every objective must end up covered by at least one knowledge point.
2. **教学内容/学时分配 (Teaching Contents)** → the primary source of knowledge points; major chapters typically become first-level 知识域 groupings, with each sub-topic inside a chapter becoming one atomic knowledge point (split further than feels natural — don't bundle distinct legal provisions/ideas into one row).
3. **重点/难点 (Key & Difficult Topics)** → drive the Importance/Difficulty ratings (see below).
4. **课程考核评价方式 (Assessment)** → infer the required competency level per relevant knowledge point using concrete action verbs (Explain / Analyze / Compare / Evaluate / Apply / Define) — avoid vague verbs like "understand" or "be familiar with" unless no better verb fits. Assessment sections also often hide standalone 方法工具-domain knowledge points (case-analysis frameworks, research/writing methods) — extract these too, don't skip them as "administrative."
5. **参考材料 (References)** → attach relevant readings to the knowledge points they support, and extract a small number of extension knowledge points from a reference's known theoretical contribution if the title clearly implies one (don't force it for generic titles).

**知识域 (Knowledge Domain)** — each knowledge point gets exactly ONE, from this fixed list (don't invent new ones unless a course genuinely can't fit any of these — ask the user first):

| 知识域   | 含义               |
| -------- | ------------------ |
| 基础理论 | 学科背景、发展历史 |
| 核心概念 | 定义、术语         |
| 基本原理 | 原则、理论         |
| 制度体系 | 法律、规范、组织   |
| 方法工具 | 分析方法、模型     |
| 应用实践 | 案例、实践         |
| 热点问题 | 当前研究热点       |
| 中国实践 | 中国经验、政策     |
| 前沿发展 | 新规则、新趋势     |

**知识点类型 (Knowledge Point Type)** — each knowledge point gets exactly ONE:

| 类型     |
| -------- |
| 概念     |
| 理论     |
| 原则     |
| 制度     |
| 法律文件 |
| 国际组织 |
| 治理机制 |
| 方法     |
| 模型     |
| 案例     |
| 热点问题 |
| 中国实践 |
| 专业术语 |

**Ratings:** 重点 chapters/topics → Importance ★★★★★ for their knowledge points (default ★★★☆☆ otherwise). 难点 chapters/topics → Difficulty ★★★★★ (default ★★★☆☆ otherwise). These are the same 重点/难点 signals as before, just expressed as star ratings instead of a plain flag.

**IDs:** assign each knowledge point a sequential ID `K001`, `K002`, ... in extraction order (roughly chapter order). IDs must stay stable if the user asks for revisions later — don't renumber existing points, only append new ones.

**Relationships:** for each knowledge point, identify (where genuinely applicable, not forced):

- **Prerequisites** — knowledge points that should logically be learned first (e.g. a general legal framework before a specific instrument built on it).
- **Successors** — knowledge points that naturally build on this one.
- **Related Knowledge** — cross-domain connections that aren't strictly prerequisite/successor (e.g. a China-practice point relating to the global-institution point it responds to).

Build these from the logical/legal/conceptual structure of the field, not just chapter order — e.g. `国际法通论 → UNCLOS → RFMO → IUU 捕鱼` is a conceptual dependency chain, not merely a page sequence.

**Tags:** prefer bilingual tags where the term has a standard English equivalent (e.g. `UNCLOS`, `联合国海洋法公约`).

**Cross-reference against 课程目标:** every knowledge point should note which numbered course objective(s) it supports.

**Output 1 — Knowledge Map (student-facing):** `01_[课程名]_知识地图.md`

Audience: students. Purpose: a learning roadmap — readable, not exhaustive on every schema field.

```
# [课程名称 Course Title]

## 课程概况
一两句话概述本课程是什么、面向谁、在专业中的定位。（先文字，后表格。）

| 项目 | 内容 |
| ---- | ---- |
| 课程名称 | ... |
| 课程代码 | ...（如有） |
| 学分 | ... |
| 总学时 | ...（含理论/实践分配，如有） |
| 授课对象 | ... |
| 先修课程 | ... |
| 开课院系 | ...（如有） |

## 课程目标
一两句话说明课程整体育人目标。（先文字，后表格。）

| 序号 | 课程目标 | 支撑毕业要求/能力 |
| ---- | -------- | ----------------- |
| 目标1 | ... | ...（如大纲未给出可留空或省略此列） |
| 目标2 | ... | ... |

## 知识点总览表
| ID | 知识点 | 知识域 | 类型 | 重要程度 | 难度 | 支撑课程目标 | 一句话说明 |
(one row per knowledge point — the human-scannable summary; full detail lives in the knowledge base file)

## 知识地图（按知识域分组，强调学习顺序而非章节顺序）
Course
├── 知识域 A
│      ├── 知识点 (ID)
│      ├── 知识点 (ID)
├── 知识域 B
└── ...

## 专业术语中英对照表
## 考核方式
## 教学方法
## 参考材料
```

**Output 2 — Knowledge Base (AI/RAG-facing):** `02_[课程名]_知识库.md`

Audience: AI retrieval systems (RAG / GraphRAG / agent memory), and anyone wanting full traceability. Purpose: one fully-specified knowledge object per point, machine-parseable and consistent.

```
# [课程名称] 知识库

## 知识点 K001

### ID
K001

### 名称 Name
...

### 知识域 Knowledge Domain
...

### 类型 Type
...

### 重要程度 Importance
★★★★★

### 难度 Difficulty
★★★☆☆

### 来源 Sources
- 课程目标: ...
- 教学章节: 第N章
- 参考材料: ...(if applicable)

### 学习成果 Learning Outcomes
Students should be able to:
- [action verb] ...
- [action verb] ...

### 核心内容 Core Knowledge
Concise, accurate summary of the essential concept/principle/mechanism/framework — suitable for later expansion, not a copy of the syllabus line.

### 前置知识 Prerequisites
- K0xx 知识点名 (if any)

### 后续知识 Successors
- K0xx 知识点名 (if any)

### 关联知识 Related Knowledge
- K0xx 知识点名 (if any)

### 推荐阅读 Recommended Reading
- ...

### 标签 Tags
Tag1, 标签1, Tag2, 标签2
```

(repeat one `## 知识点 K00N` block per extracted point, in ID order)

**Output 3 — Knowledge Point Deep-Dive (student/teacher-facing):** `03_[课程名]_知识点详解.md`

Audience: students and teachers who want to actually *learn / teach* the material, not just navigate it. Purpose: a readable, prose deep-dive that explains each knowledge point in depth — the substance that the map (an overview) and the base (schema fields) deliberately leave out. Unlike those two, this document is meant to be read start to finish.

Open with the **same 课程概况 + 课程目标 text-then-table treatment as Output 1** (reuse the identical prose and tables — don't reword them), so the deep-dive stands on its own as a readable document. Then cover every knowledge point, in ID order, grouped under their 知识域 headings.

```
# [课程名称 Course Title] 知识点详解

## 课程概况
一两句话概述本课程（与知识地图一致）。（先文字，后表格。）

| 项目 | 内容 |
| ---- | ---- |
| 课程名称 | ... |
| 学分 | ... |
| 总学时 | ... |
| 授课对象 | ... |
| 先修课程 | ... |

## 课程目标
一两句话说明课程整体育人目标（与知识地图一致）。（先文字，后表格。）

| 序号 | 课程目标 | 支撑毕业要求/能力 |
| ---- | -------- | ----------------- |
| 目标1 | ... | ... |
| 目标2 | ... | ... |

## 知识域：基础理论

## K001 知识点名称
*知识域 · 类型 · 重要程度 ★★★★★ · 难度 ★★★☆☆ · 支撑课程目标 N*

**定义**
Plain-language definition of the concept.

**为什么重要 / 学习意义**
Why it matters in this course and the field.

**详细讲解**
The core of this document — a thorough, accurate explanation of the point:
background, the key ideas and how they fit together, distinctions, common
misconceptions. Multiple paragraphs where the point warrants it. This is where
actual subject-matter content lives.

**示例 / 案例**
A concrete example or case illustrating the point (use the syllabus's own if it
names one; otherwise construct a fitting, accurate one).

**重点与难点提示**
What students most often need to focus on or get wrong here (informed by the
重点/难点 ratings).

**关联**
- 前置: K0xx 名称 — one line on why
- 后续: K0xx 名称 — one line on why
```

(repeat one `## K00N` block per point, in ID order)

**Sourcing caveat — this output is deliberately different from the other two.** The map and base are strictly syllabus-traceable (nothing invented). The deep-dive, by design, *elaborates*: explaining a point in depth requires drawing on established, general domain knowledge that goes beyond its one-line syllabus entry — that is expected and wanted here. But keep every claim factually accurate, do **not** fabricate *course-specific* specifics the syllabus doesn't state (a professor's exact wording, exam weightings, "the textbook says on p.42…"), and flag genuine uncertainty inline with `**（需核实 / verify against course materials）**` rather than stating a shaky claim as fact (use `**bold**`, not `*italic*` — the docx converter only renders inline bold, so single-asterisk spans would leak literal `*` into the Word file).

Keep all three outputs **in Chinese** (matching source language) unless the user asks for translation, though field labels/schema names may stay bilingual as shown for AI-compatibility.

#### Type B — Lecture notes, transcripts, articles (actual course content)

For material that contains the substantive teaching content itself (not just a curriculum plan), produce a concept-level outline:

```
# [Course / Topic Title]

## Overview
1-3 sentences: what this material covers and why it matters.

## [Major Concept 1]
- **Definition:** clear, plain-language definition
- **Why it matters:** the point/purpose behind the concept
- **Example:** a concrete example illustrating it (use the source's own example if given, otherwise construct one that fits the same level)
- **Key details / sub-points:** bullet list of supporting facts, formulas, distinctions, etc.

## [Major Concept 2]
... (same shape)

## Key Terms Glossary
- **Term:** short definition
(only include terms not already fully defined above)

## Summary / Takeaways
- 3-6 bullet points capturing the most important things to remember
```

Guidelines for Type B extraction:

- Preserve the material's own structure/sequence where it makes sense, but reorder for clarity if the source is disorganized.
- Prioritize concepts the source material emphasizes or repeats — these are usually the "important" ones.
- Keep definitions in your own words; do not copy long verbatim passages (see copyright guidance — quotes under 15 words, one per source, otherwise paraphrase).
- Include examples for every major concept. If the source doesn't give one, construct a simple original example rather than skipping it.
- Flag anything genuinely ambiguous or contradictory in the source with a short inline note like `*(source is unclear here)*` rather than silently resolving it.

If unsure which type applies, look at the table of contents / headings first — if they read like an administrative curriculum plan (objectives, hours, assessment %), it's Type A; if they read like actual subject-matter explanation, it's Type B. When genuinely ambiguous, ask the user.

### 3. Produce the outputs

All files are written into your **current working directory** — the shared session
workspace (`workspaces/sess_<timestamp>/`). Everything you write there persists on disk
and is visible to sibling subagents and to the user; there is no separate "publish" step.
When done, report the created filenames (relative to the workspace) in your final message.

The bundled scripts live under this skill's own directory, not the workspace — invoke them
by absolute path. This skill's directory is `skills/course-knowledge-extraction/` at the
repo root; from a subagent shell that resolves to `../../skills/course-knowledge-extraction/`
relative to the session workspace, but prefer the absolute repo path if you know it.

**For Type A (syllabus):**

- Write `01_[课程名]_知识地图.md`, `02_[课程名]_知识库.md`, and `03_[课程名]_知识点详解.md` into the workspace as described above.
- Convert the two human-facing docs — the knowledge map **and** the deep-dive — to `.docx` with a bundled script — **don't write custom docx-generation code from scratch each time**. **Prefer the Python script** (`python-docx` is the reliable runtime here; the Node script needs the `docx` npm package, which is often not installed):
  ```bash
  # Python (preferred) — requires `python-docx` (uv add python-docx, or pip install if outside uv)
  # --pdf also emits a .pdf; --preview N renders the first N pages to PNG for a visual spot-check.
  python <skill_dir>/scripts/md_to_docx.py 01_[课程名]_知识地图.md   01_[课程名]_知识地图.docx   --pdf --preview 2
  python <skill_dir>/scripts/md_to_docx.py 03_[课程名]_知识点详解.md 03_[课程名]_知识点详解.docx --pdf --preview 2

  # Node.js (only if `docx` npm package is available) — same flags
  node <skill_dir>/scripts/md_to_docx.js 01_[课程名]_知识地图.md 01_[课程名]_知识地图.docx --pdf --preview 2
  ```

  Both take the same input/output arguments and flags. The Python version uses a **dual-font setup** by default — `Calibri` for Latin characters/numbers, `Microsoft YaHei` for CJK characters (override with `--latin-font` / `--cjk-font`) — and automatically compacts wide tables (6+ columns, e.g. the knowledge-point overview table) with a smaller font, tighter row spacing, and content-aware column widths (fixed table layout, CJK-aware) so they don't sprawl across many pages. The Node version uses a single font for everything by default (`--font` to override) and mirrors the Python heading hierarchy. Both handle headings, pipe tables, bullet lists, the fenced-code-block tree diagram, and `**bold**` spans — the deep-dive stays within this same subset (headings, bold labels, bullets, paragraphs). The knowledge base file (`02_...`) is the one exception: it stays markdown-only, since it's for machine consumption and doesn't need a Word version.
- **PDF is an optional deliverable.** `--pdf` renders the `.docx` to `.pdf` via LibreOffice (`soffice`) — a good default to include, since a PDF renders identically everywhere regardless of the reader's Word version or installed fonts. Deliver PDFs for the same two human-facing docs (`01_...`, `03_...`); the knowledge base (`02_...`) stays markdown-only. If `soffice` isn't on PATH the script prints a note and still leaves the `.docx` — no failure.
- **Verify the result** with the `--preview N` flag, which renders the first N pages of the PDF to `NAME_preview-*.png` (needs `pdftoppm`; present in the default environment) — read those PNGs to confirm layout. Worth it especially for very long tables or deeply nested tree diagrams the scripts weren't tuned for. `--preview` implies `--pdf`. If the tools aren't present the script degrades gracefully; fall back to sanity-checking the `.md`. The preview PNGs are throwaway — delete them once you've checked, they aren't deliverables.
  - If the markdown has content beyond what the scripts handle (e.g. nested sub-bullets, block quotes), either extend the relevant script or fall back to writing custom docx code for that document — don't silently drop content.

**For Type B (lecture content):**

- Produce the `.md` outline in the workspace and convert it to `.docx` with a bundled script above (Python preferred), using the same `--pdf --preview N` flags to emit the PDF and verify.

### 4. Naming

Use the course/topic title extracted from the material for the `[课程名]` placeholder, e.g. for a course titled "XX 概论" the files would be `01_XX概论_知识地图.md`, `02_XX概论_知识库.md`. If no clear title exists, ask the user for one or use a short descriptive slug of the content.

## Quality checklist (Type A)

Before presenting, confirm the output is:

- **Traceable** — every knowledge point cites its syllabus source(s)
- **Non-duplicative** — student-report/discussion chapters (专题讨论) revisit existing points rather than creating near-duplicates
- **Schema-consistent** — every knowledge point has all fields, uses only the fixed 知识域/知识点类型 vocab, and IDs are sequential with no gaps
- **Not verbatim** — core content is summarized/paraphrased, not copied from the syllabus
- **Not invented** (map & base) — the knowledge map and knowledge base add nothing not implied by the source; the deep-dive (`03_...`) may elaborate with accurate general domain knowledge, but still fabricates no *course-specific* specifics and flags uncertainty (see its sourcing caveat)

## Notes

- **Bundled scripts:** `scripts/md_to_docx.py` (preferred) and `scripts/md_to_docx.js` (relative to this skill's directory) both convert this skill's markdown output into `.docx`, and — with `--pdf` / `--preview N` — optionally into a `.pdf` (via LibreOffice/`soffice`) plus PNG page previews (via `pdftoppm`) in the same call. The Python version requires `python-docx` (present in this environment); the Node version requires the `docx` npm package (often not installed) — prefer Python unless you've confirmed the npm package is available. Both are intentionally narrow converters tuned to this skill's own markdown shape (headings, pipe tables, bullets, one fenced code block for the tree diagram, `**bold**`) — not general Markdown-to-Word tools, and they emit the same heading hierarchy and share the same CLI flags. The PDF/preview steps shell out to the system `soffice`/`pdftoppm` (each PDF conversion uses an isolated LibreOffice profile so parallel subagents don't collide) and no-op with a printed note if those tools are absent.
- This skill is for **text-based** material (notes, transcripts already in text form, articles, syllabi). If the user has raw video/audio or slide decks, first get the content into text, then apply this workflow.
- If the material is very long (e.g. a syllabus with 15+ chapters), consider whether to split the knowledge base file by module — check with the user if unsure.
- The 知识域/知识点类型 taxonomies are the current default classification scheme (well-suited to law/governance/social-science courses). If a course's subject matter doesn't fit well, ask whether to adapt before proceeding rather than force-fitting content into a poor category.
- Knowledge points extracted from a syllabus describe *what will be taught*, not the actual explanatory content. The knowledge map and knowledge base stay at that roadmap / coverage level; the **deep-dive (`03_...`)** is where explanatory content actually gets generated, elaborated from general domain knowledge. If the user later provides real lecture notes/textbook content for these same points (Type B input), fold it into the deep-dive's 详细讲解 (and the 核心内容 field of the matching knowledge-base entries) to replace general elaboration with sourced content.
