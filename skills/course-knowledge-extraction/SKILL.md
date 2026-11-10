---
name: course-knowledge-extraction
description: "Use when the user provides course material — teaching syllabi (教学大纲), lecture notes, transcripts, articles — and wants it summarized, organized, or turned into study notes or a knowledge base. Accepts the material as pasted text, Markdown, or a .docx/.pdf syllabus (converted to Markdown first via a bundled script). Triggers: 'course notes', 'lecture notes', 'study guide', 'syllabus', '教学大纲', '知识点', '知识库', 'knowledge map', 'summarize this course', 'extract key concepts', or any text-based educational content wanting a structured breakdown. Produces, for a syllabus, a student-facing knowledge map and a readable knowledge-point deep-dive (both with matching .docx) plus a machine-readable knowledge base; for lecture content, a concept-and-example study outline. A second mode turns an existing knowledge-point deep-dive (03_...) into a class lesson plan (教案): the teacher selects the knowledge points for a session and gets a time-allocated 教案 (.md + .docx) — triggers include '教案', 'lesson plan', '备课', '这次课/本学时', '选中知识点生成教案'. Preserves source language unless translation is requested. Not for one-line document questions or quick answers."
acceptance:
  - id: deliverables_exist
    describe: "The expected deliverables exist in the workspace and are non-empty: a knowledge map (.md + matching .docx), a machine-readable knowledge base (.md), and a knowledge-point deep-dive (.md + matching .docx)."
    check_mode: mechanical
  - id: knowledge_base_populated
    describe: "The knowledge base lists real extracted knowledge points (IDs, names, domains, ratings), not a template or empty scaffold."
    check_mode: mechanical
  - id: faithful_to_source
    describe: "Knowledge points are faithful to the source material — no invented facts, no hallucinated concepts absent from the syllabus."
    check_mode: judgment
  - id: language_preserved
    describe: "Outputs preserve the source language unless the user asked for translation."
    check_mode: judgment
---
# Course Knowledge Extraction

Turns raw, text-based course material (teaching syllabi, notes, transcripts, articles, readings) into structured knowledge outputs — for syllabi, a student-facing knowledge map, a readable knowledge-point deep-dive, and a machine-readable knowledge base; for lecture content, a concept-level study outline.

## Workflow

### 1. Get the source material

- If the user referenced a file, read it with your filesystem/read tools; if a path is relative, it resolves against the session workspace (your cwd).
- If the user pasted text directly, use that.
- **Input scope & DOCX intake:** the extraction workflow operates on **text/Markdown**. Everything downstream (type detection, section mining, knowledge-point extraction) reads Markdown, so any non-text source is converted to Markdown *first*, as an explicit step — then the rest of the workflow is identical regardless of the original format.
  - **DOCX** (the common case for a 教学大纲): convert it with the bundled **`scripts/docx_to_md.py`** — **don't hand-roll docx parsing or eyeball the binary**:

    ```bash
    # Produces a faithful, structure-preserving Markdown transcription.
    # Pass --title with the course name so a Word Title-styled heading isn't lost.
    python <skill_dir>/scripts/docx_to_md.py 大纲.docx 00_[课程名]_源大纲.md --title "[课程名]"
    ```

    Engine is `auto`: it uses **pandoc** if present (clean GFM, tables → pipe tables, CJK-safe) and falls back to **python-docx** (`uv add python-docx`) otherwise — force either with `--engine pandoc|python-docx`. At least one must be installed.
  - **PDF:** `pdftotext file.pdf -` (or the `pdf`/`pdf-reading` skill) to get text, then proceed.
- **What the converted Markdown should contain (the intermediate contract).** `docx_to_md.py` is a *transcriber, not an extractor* — it must preserve, not summarize. A good `00_..._源大纲.md` keeps: the **course title** (H1); **every section** of the syllabus (基本信息 / 课程目标 / 教学内容·学时分配 / 考核方式 / 教学方法 / 参考材料); and — most importantly — the syllabus's **tables as pipe tables**, since the 教学内容 table (chapters × 学时 × 重点·难点) and the 课程目标 table are the primary things the extraction step mines. Do a quick read of this file before extracting: if a critical table came through garbled (e.g. heavy merged cells flattened badly), fix that cell in the `.md` by hand rather than extracting from broken structure. Treat this file as an intermediate working artifact — deliver it too if useful, but its job is to feed step 2.
- If material is split across multiple files/sources, convert/read all of them before extracting — don't summarize partial input.

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
| 原理     |
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

## Mode 2 — Lesson-plan generation (教案)

The workflow above (Mode 1) turns a syllabus into knowledge artifacts. **Mode 2 turns
those artifacts back into a teachable class plan.** A course is taught over many sessions
(e.g. 32 学时 at 2 学时/次 ≈ 16 次课); for a given session the teacher **selects the
knowledge points to cover** and wants a 教案 for exactly those points. The selection is the
only thing that changes the shape: 3 points × 2 学时 → a deep new-teaching plan; every point
× 2 学时 → a review/synthesis plan for the final session. Same engine, different `--select`.

**Input is the deep-dive (`03_...`), which is the sole source of truth.** The teacher edits
and corrects `03_..._知识点详解.docx`; the machine-readable knowledge base (`02_...`) is often
absent or stale, so Mode 2 reads **only Output 3**. Because the corrected content lives in
`.docx`, the first step is always to re-ingest it to Markdown.

### Pipeline

```
① 回灌   03_*.docx → docx_to_md.py → 03.md            (authoritative content is in .docx)
② 解析   parse_knowledge.py 03.docx → JSON            (points + course context)
③ 选点   --select K001,K008,...                        (the teacher's chosen points)
④ 教学设计  order by prerequisites; allocate time by importance/difficulty & plan_mode
⑤ 渲染   author 04_[课程名]_[课时名]_教案.md
⑥ 转换   md_to_docx.py --pdf --preview 2 → 04_..._教案.docx
```

**`scripts/parse_knowledge.py`** parses a deep-dive (`.docx` — converted internally — or a
pre-converted `.md`) into structured JSON with **English keys only** (no redundant fields):
`course` = `overview` + `profile` (`course_name`, `english_name`, `college`, `major`,
`audience`, `total_hours`) + `objectives_intro` + `objectives`, plus one
object per point (`id`, `knowledge_domain`, `point_type`, `importance`/`difficulty` as 1–5,
`supports_objectives`, `definition`, `learning_significance`, `detailed_explanation`,
`examples`, `key_points_and_difficulties`, `prerequisites`/`successors`/`related`).

```bash
# 0) LINT FIRST — surface teacher-editing slips (dup IDs, bad/compound 知识域, wrong 类型,
#    missing 定义, dangling relations) before building a 教案 on inconsistent data.
python <skill_dir>/scripts/parse_knowledge.py --lint 03_[课程名]_知识点详解.docx
# fix any ERROR in the source .docx first (a compound 知识域, a 类型 slot holding a 知识域, etc.)

# 1) parse only the selected points to JSON
python <skill_dir>/scripts/parse_knowledge.py 03_[课程名]_知识点详解.docx \
    --select K001,K008,K014 --pretty -o points.json
```

`learning_outcomes` is **not** in the JSON (it lived in the absent knowledge base) — synthesise
the 能力目标 from each point's `definition` + `learning_significance` + `detailed_explanation`.

### Ask the teacher for the session parameters

Selection alone doesn't fix the plan's shape — also get (or infer, then confirm):

- **学时数 & 每学时分钟** (e.g. 2 学时 × 45 min = 90 min) — drives time allocation.
- **课时名/课题** — goes in the title and filename.
- **plan_mode** — `新授` (deep, per-point teaching), `复习综合` (many points, survey + 重难点
  only), or `单元总结` (a 知识域's points, emphasise connections). If points-per-minute is high,
  it's implicitly a 复习综合 plan — don't try to deep-teach 20 points in 90 minutes.
- **授课对象** — defaults to the 03 课程概况 table's 授课对象 if the teacher doesn't specify.

### The 教案 template (Output 4)

```
# [课程名] 教案 · [课时名]

## 基本信息
| 项目 | 内容 |
| ---- | ---- |
| 课程名称 | ... |
| 授课课题 | [课时名] |
| 学时 | N 学时（约 M 分钟） |
| 授课对象 | ... |
| 涉及知识点 | K001 名称、K008 名称 … |

## 教学目标
- **知识目标**：学生能够…（源自选中点的 定义 / 核心内容）
- **能力目标**：学生能够…（行为动词，源自 学习意义 + 详细讲解，合成 learning_outcomes）
- **素养目标**：…（源自 中国实践 / 思政点 / 该点支撑的 课程目标）

## 教学重点与难点
- **重点**：K0xx 名称 — …（importance ★★★★★ 的点 + 重点难点提示）
- **难点**：K0xx 名称 — …（difficulty ★★★★★ 的点 + 重点难点提示）

## 教学方法
讲授法 / 案例讨论 / 小组研讨 …（依知识点类型选：概念→讲授，案例→讨论，方法→演示练习）

## 教学过程（共 M 分钟）

### 导入（约 X 分钟）
情境/问题导入，衔接前置知识点。

### 新课讲授
#### 环节一 · K001 [名称]（约 X 分钟）
- **讲授内容**：…（提炼自 详细讲解 的要点，非照抄）
- **师生活动**：…（提问 / 讨论 / 举例，用该点的 示例/案例）
- **设计意图**：…（为什么这样教，呼应重点难点）

#### 环节二 · K008 [名称]（约 X 分钟）
…（每个选中知识点一个环节，按前置关系排序）

### 课堂小结（约 X 分钟）
串联本节知识点，回扣教学目标。

### 作业 / 思考题
…（依重难点设计）

## 板书设计
选中知识点的结构与关联（可用 knowledge relations 画简图）。

## 教学反思
（课后填写，留空）
```

### Field mapping (教案栏目 ← 03 字段)

| 教案栏目              | 来源                                                                   |
| --------------------- | ---------------------------------------------------------------------- |
| 基本信息 / 涉及知识点 | Request 参数 + 选中点的`id`·`name`                                |
| 知识目标              | 选中点的`definition`                                                 |
| 能力目标              | 合成自`learning_significance` + `detailed_explanation`（行为动词） |
| 素养目标              | 中国实践/思政点 +`supports_objectives` → 课程目标文本               |
| 教学重点              | `importance` 高的点 + `key_points_and_difficulties`                |
| 教学难点              | `difficulty` 高的点 + `key_points_and_difficulties`                |
| 教学方法              | 依`point_type` 推荐                                                  |
| 教学过程各环节        | `detailed_explanation` + `examples`，按 `prerequisites` 排序     |
| 板书设计              | 选中点名称 +`prerequisites`/`successors`/`related` 关系          |

### Time-allocation heuristics (the core of a good 教案)

The hard part is fitting the selected points into the available minutes:

- Reserve ~10–15% for **导入** and ~10% for **小结**; the rest is the 讲授 budget.
- Split the 讲授 budget across selected points **weighted by `importance + difficulty`** — a
  ★★★★★/★★★★★ point earns more minutes than a ★★★☆☆/★★★☆☆ one.
- **Sanity-check density.** If the 讲授 budget ÷ point-count leaves only a couple of minutes
  each, it's a `复习综合` plan: group points by 知识域, cover only 重点/难点 and connections,
  don't author a full 环节 per point.
- **Order by dependency**, not ID: use `prerequisites`/`successors` to sequence 环节 so a point
  is taught after the points it builds on.
- Every 环节's minutes must sum (with 导入 + 小结) to the total M minutes — state each 环节's
  minutes and make the arithmetic add up.

### Naming & conversion

`04_[课程名]_[课时名]_教案.md`, converted to `.docx` with the same output-side script as Mode 1:

```bash
python <skill_dir>/scripts/md_to_docx.py 04_[课程名]_[课时名]_教案.md 04_[课程名]_[课时名]_教案.docx --pdf --preview 2
```

### Quality checklist (Mode 2)

- **Complete** — `04_..._教案.md` + `.docx` exist and are non-empty.
- **Covers the selection** — every selected knowledge point appears in 教学过程 (a 环节, or a
  named item in the 复习综合 grouping) and is reflected in at least one 教学目标.
- **Time adds up** — the per-环节 minutes plus 导入/小结 equal the stated total M minutes.
- **Faithful to `03_...`** — teaching content is drawn from the selected points' 详细讲解/示例;
  it may elaborate with accurate general domain knowledge but invents no course-specific
  specifics the deep-dive doesn't state.
- **Objectives traceable** — 素养目标 map back to the course objectives the points support.

## Notes

- **Bundled scripts — input side:** `scripts/docx_to_md.py` converts a source `.docx` (a 教学大纲) *into* Markdown at the start of the workflow — the input-side companion to the output-side `md_to_docx` scripts. It's a faithful transcriber (preserves sections and tables; does **not** extract), using pandoc when available and falling back to `python-docx`. It's the one bundled script that is a *reasonably general* docx→md converter rather than being tuned to this skill's own markdown shape.
- **Bundled scripts — output side:** `scripts/md_to_docx.py` (preferred) and `scripts/md_to_docx.js` (relative to this skill's directory) both convert this skill's markdown output into `.docx`, and — with `--pdf` / `--preview N` — optionally into a `.pdf` (via LibreOffice/`soffice`) plus PNG page previews (via `pdftoppm`) in the same call. The Python version requires `python-docx` (present in this environment); the Node version requires the `docx` npm package (often not installed) — prefer Python unless you've confirmed the npm package is available. Both are intentionally narrow converters tuned to this skill's own markdown shape (headings, pipe tables, bullets, one fenced code block for the tree diagram, `**bold**`) — not general Markdown-to-Word tools, and they emit the same heading hierarchy and share the same CLI flags. The PDF/preview steps shell out to the system `soffice`/`pdftoppm` (each PDF conversion uses an isolated LibreOffice profile so parallel subagents don't collide) and no-op with a printed note if those tools are absent.
- **Bundled scripts — knowledge re-ingestion (Mode 2):** `scripts/parse_knowledge.py` parses a `03_..._知识点详解` (`.docx`, converted internally via `docx_to_md`, or a pre-converted `.md`) back into structured JSON (course context + one object per knowledge point), the input engine for lesson-plan generation. `--select K001,K008` keeps only chosen points; `--lint` (accepts a file **or a directory** of courses) reports source-data inconsistencies — duplicate IDs, non-vocab/compound 知识域, a 类型 slot mistakenly holding a 知识域, missing 定义, dangling relation references — and exits non-zero on any ERROR, so it doubles as a pre-flight check on teacher-corrected deep-dives. `--batch <dir>` exports one `<课程名>_knowledge.json` per course (beside each `03_...` docx, or under `--out-dir`), lint-gated so a course with ERRORs is skipped rather than exported dirty — the way to persist a whole course library to JSON and re-run whenever a deep-dive is corrected. Unlike the `02_...` knowledge base it does not emit `learning_outcomes` (synthesise those downstream).
- This skill is for **text-based** material (notes, transcripts already in text form, articles, syllabi). If the user has raw video/audio or slide decks, first get the content into text, then apply this workflow.
- If the material is very long (e.g. a syllabus with 15+ chapters), consider whether to split the knowledge base file by module — check with the user if unsure.
- The 知识域/知识点类型 taxonomies are the current default classification scheme (well-suited to law/governance/social-science courses). If a course's subject matter doesn't fit well, ask whether to adapt before proceeding rather than force-fitting content into a poor category.
- Knowledge points extracted from a syllabus describe *what will be taught*, not the actual explanatory content. The knowledge map and knowledge base stay at that roadmap / coverage level; the **deep-dive (`03_...`)** is where explanatory content actually gets generated, elaborated from general domain knowledge. If the user later provides real lecture notes/textbook content for these same points (Type B input), fold it into the deep-dive's 详细讲解 (and the 核心内容 field of the matching knowledge-base entries) to replace general elaboration with sourced content.
