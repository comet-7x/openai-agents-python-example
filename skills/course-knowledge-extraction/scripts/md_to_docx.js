#!/usr/bin/env node
/**
 * md_to_docx.js
 *
 * Converts a knowledge-map markdown file (as produced by the
 * course-knowledge-extraction skill) into a formatted .docx.
 *
 * Handles the fixed subset of Markdown this skill's output actually uses:
 *   - Headings: #, ##, ### (### also used for per-item sub-headings)
 *   - Pipe tables: | a | b | ... | with a |---|---| separator row
 *   - Bullet lists: lines starting with "- " or "* "
 *   - Fenced code blocks: ``` ... ``` (rendered as monospace, e.g. the
 *     tree-style knowledge map diagram)
 *   - Inline **bold**
 *   - Plain paragraphs
 *
 * This is NOT a general CommonMark parser — it's intentionally narrow,
 * tuned to this skill's own output shape, so conversion stays predictable.
 *
 * Usage:
 *   node md_to_docx.js <input.md> <output.docx> [--font "Microsoft YaHei"]
 *
 * Requires the `docx` npm package (already available in this environment's
 * global node_modules; if running elsewhere: npm install docx).
 */

const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel,
  Table, TableRow, TableCell, WidthType, ShadingType,
} = require("docx");

function parseArgs(argv) {
  const args = argv.slice(2);
  const positional = args.filter(a => !a.startsWith("--"));
  const fontIdx = args.indexOf("--font");
  const font = fontIdx !== -1 ? args[fontIdx + 1] : "Microsoft YaHei";
  if (positional.length < 2) {
    console.error("Usage: node md_to_docx.js <input.md> <output.docx> [--font \"Font Name\"]");
    process.exit(1);
  }
  return { input: positional[0], output: positional[1], font };
}

// Split a line into runs, handling **bold** spans.
function parseInlineRuns(text, font, extraOpts = {}) {
  const runs = [];
  const re = /\*\*(.+?)\*\*/g;
  let lastIndex = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIndex) {
      runs.push(new TextRun({ text: text.slice(lastIndex, m.index), font, ...extraOpts }));
    }
    runs.push(new TextRun({ text: m[1], font, bold: true, ...extraOpts }));
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    runs.push(new TextRun({ text: text.slice(lastIndex), font, ...extraOpts }));
  }
  if (runs.length === 0) runs.push(new TextRun({ text, font, ...extraOpts }));
  return runs;
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes("-");
}

function splitTableRow(line) {
  let cells = line.trim();
  if (cells.startsWith("|")) cells = cells.slice(1);
  if (cells.endsWith("|")) cells = cells.slice(0, -1);
  return cells.split("|").map(c => c.trim());
}

function buildTable(rows, font) {
  const colCount = rows[0].length;
  const colWidth = Math.floor(9000 / colCount);
  const trows = rows.map((cells, rIdx) => {
    return new TableRow({
      children: cells.map(text => new TableCell({
        width: { size: colWidth, type: WidthType.DXA },
        shading: rIdx === 0 ? { type: ShadingType.CLEAR, fill: "D9E2F3" } : undefined,
        children: [new Paragraph({ children: parseInlineRuns(text, font, { bold: rIdx === 0 }) })],
      })),
    });
  });
  return new Table({ rows: trows, width: { size: colWidth * colCount, type: WidthType.DXA } });
}

function convert(mdText, font) {
  const lines = mdText.split(/\r?\n/);
  const children = [];
  let i = 0;
  let firstHeadingUsed = false;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (line.trim().startsWith("```")) {
      i++;
      const codeLines = [];
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      codeLines.forEach(cl => {
        children.push(new Paragraph({
          children: [new TextRun({ text: cl.length ? cl : " ", font: "Consolas", size: 18 })],
          spacing: { after: 0 },
        }));
      });
      children.push(new Paragraph({ text: "" }));
      continue;
    }

    // Heading
    const headingMatch = line.match(/^(#{1,4})\s+(.*)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const text = headingMatch[2].trim();
      if (!firstHeadingUsed && level === 1) {
        children.push(new Paragraph({ text, heading: HeadingLevel.TITLE }));
        firstHeadingUsed = true;
      } else {
        // Mirror md_to_docx.py's mapping so the two runtimes produce the same
        // heading hierarchy: # -> H1, ## -> H2, ### -> H3, #### -> H3
        // (the first # becomes the document Title, handled above).
        const map = {
          1: HeadingLevel.HEADING_1,
          2: HeadingLevel.HEADING_2,
          3: HeadingLevel.HEADING_3,
          4: HeadingLevel.HEADING_3,
        };
        children.push(new Paragraph({ text, heading: map[level] || HeadingLevel.HEADING_3 }));
      }
      i++;
      continue;
    }

    // Table: current line starts with | and next line is a separator
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const tableRows = [splitTableRow(line)];
      i += 2; // skip header + separator
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableRows.push(splitTableRow(lines[i]));
        i++;
      }
      children.push(buildTable(tableRows, font));
      children.push(new Paragraph({ text: "" }));
      continue;
    }

    // Bullet list item
    const bulletMatch = line.match(/^\s*[-*]\s+(.*)$/);
    if (bulletMatch) {
      children.push(new Paragraph({
        children: parseInlineRuns(bulletMatch[1], font),
        bullet: { level: 0 },
      }));
      i++;
      continue;
    }

    // Numbered list item
    const numberedMatch = line.match(/^\s*(\d+)\.\s+(.*)$/);
    if (numberedMatch) {
      children.push(new Paragraph({
        children: parseInlineRuns(`${numberedMatch[1]}. ${numberedMatch[2]}`, font),
      }));
      i++;
      continue;
    }

    // Horizontal rule -> spacing
    if (/^\s*---+\s*$/.test(line)) {
      children.push(new Paragraph({ text: "" }));
      i++;
      continue;
    }

    // Blank line
    if (line.trim() === "") {
      i++;
      continue;
    }

    // Plain paragraph (possibly italic *text* used as note)
    const italicMatch = line.match(/^\*([^*].*?)\*$/);
    if (italicMatch) {
      children.push(new Paragraph({ children: [new TextRun({ text: italicMatch[1], font, italics: true })] }));
      i++;
      continue;
    }

    children.push(new Paragraph({ children: parseInlineRuns(line, font) }));
    i++;
  }

  return new Document({
    styles: {
      default: { document: { run: { font, size: 21 } } },
      paragraphStyles: [
        { id: "Title", name: "Title", run: { font, size: 36, bold: true } },
        { id: "Heading1", name: "Heading 1", run: { font, size: 26, bold: true } },
        { id: "Heading2", name: "Heading 2", run: { font, size: 23, bold: true } },
        { id: "Heading3", name: "Heading 3", run: { font, size: 21, bold: true, italics: true } },
      ],
    },
    sections: [{
      properties: {
        page: { size: { width: 12240, height: 15840 }, margin: { left: 900, right: 900 } },
      },
      children,
    }],
  });
}

async function main() {
  const { input, output, font } = parseArgs(process.argv);
  const mdText = fs.readFileSync(input, "utf-8");
  const doc = convert(mdText, font);
  const buf = await Packer.toBuffer(doc);
  fs.writeFileSync(output, buf);
  console.log(`Converted ${input} -> ${output} (font: ${font})`);
}

main().catch(err => {
  console.error("Conversion failed:", err);
  process.exit(1);
});