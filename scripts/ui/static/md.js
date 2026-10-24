// Small dependency-free markdown renderer for detail screens.
// parseMarkdown(text) -> Block[] is pure data (no DOM); renderMarkdown(text, opts) turns it into a DocumentFragment.
// Text is only ever set as text nodes (no HTML parsing), so tags in the source show up as literal characters.
//
// Block  = { t: "heading", level: 1..6, c: Inline[] }      (rendered as h3..h6: the page owns h1/h2)
//        | { t: "p", c: Inline[] }                          (source line breaks kept as { t: "br" })
//        | { t: "ul" | "ol", start: number, items: Block[][] }
//        | { t: "quote", c: Block[] }
//        | { t: "code", lang: string, v: string }
//        | { t: "hr" }
//        | { t: "table", head: Inline[][], rows: Inline[][][] }   (every row padded to the widest row)
// Inline = { t: "text", v } | { t: "br" } | { t: "code", v }
//        | { t: "strong" | "em", c: Inline[] }
//        | { t: "ref", id, c }       link to videos/<id>.md, resolved by opts.refHref
//        | { t: "link", href, c }    https YouTube link only; any other link becomes plain text "label (url)"
// renderMarkdown opts: refHref(id) -> href for ref links; dropTitle (string) drops a leading "# <title>" heading.
// Not supported on purpose: images, raw HTML, indented code, setext headings, footnotes.
import { h } from "./dom.js";

const MAX_BLOCK_DEPTH = 8;
const MAX_INLINE_DEPTH = 5;
const YOUTUBE_HOSTS = new Set(["www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"]);
const ESCAPABLE = "\\`*_{}[]()#+-.!|>~<";

const FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)/;
const HEADING_OPEN = /^ {0,3}(#{1,6})[ \t]+/;
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}> ?/;
const LIST = /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+|$)/;
const DELIM_CELL = /^:?-+:?$/;
const VIDEO_MD = /(?:^|\/)videos\/([^/?#\s]+)\.md$/;
const HAS_SCHEME = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;
const WORD_CHAR = /[\p{L}\p{N}]/u;

// ---- block parsing ------------------------------------------------------------------------

export function parseMarkdown(text) {
  const src = String(text === null || text === undefined ? "" : text).replace(/\r\n?/g, "\n");
  const lines = src.split("\n").map((ln) => ln.replace(/^\t+/, (m) => "    ".repeat(m.length)));
  return parseBlocks(lines, 0);
}

const isBlank = (ln) => ln.trim() === "";

function indentOf(ln) {
  let n = 0;
  while (n < ln.length && ln.charCodeAt(n) === 32) n++;
  return n;
}

// "title ###" -> "title" (manual scan: a regex here backtracks quadratically on long runs)
function stripClosingHashes(s) {
  let end = s.trimEnd().length;
  let k = end;
  while (k > 0 && s[k - 1] === "#") k--;
  if (k < end && k > 0 && (s[k - 1] === " " || s[k - 1] === "\t")) end = k;
  return s.slice(0, end).trim();
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") { cur += "|"; i++; }
    else if (ch === "|") { cells.push(cur.trim()); cur = ""; }
    else cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

function isDelimiterRow(line) {
  if (!line.includes("|")) return false;
  return splitRow(line).every((c) => DELIM_CELL.test(c));
}

function tableStartsAt(lines, i) {
  if (i + 1 >= lines.length || !lines[i].includes("|") || !isDelimiterRow(lines[i + 1])) return false;
  return splitRow(lines[i]).length === splitRow(lines[i + 1]).length;
}

function startsBlock(lines, i) {
  const ln = lines[i];
  return FENCE.test(ln) || HEADING_OPEN.test(ln) || HR.test(ln) || QUOTE.test(ln) || LIST.test(ln) || tableStartsAt(lines, i);
}

function inlineLines(lines) {
  const out = [];
  lines.forEach((ln, idx) => {
    if (idx > 0) out.push({ t: "br" });
    out.push(...parseInline(ln.trim(), 0));
  });
  return out;
}

function parseBlocks(lines, depth) {
  const blocks = [];
  if (depth >= MAX_BLOCK_DEPTH) {
    const flat = lines.filter((ln) => !isBlank(ln));
    return flat.length ? [{ t: "p", c: inlineLines(flat) }] : blocks;
  }
  let i = 0;
  const n = lines.length;
  while (i < n) {
    const ln = lines[i];
    if (isBlank(ln)) { i++; continue; }

    const fence = FENCE.exec(ln);
    if (fence) {
      const mark = fence[1];
      const body = [];
      i++;
      while (i < n) {
        const close = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
        if (close && close[1][0] === mark[0] && close[1].length >= mark.length) { i++; break; }
        body.push(lines[i]);
        i++;
      }
      blocks.push({ t: "code", lang: fence[2] || "", v: body.join("\n") });
      continue;
    }

    const head = HEADING_OPEN.exec(ln);
    if (head) {
      const content = stripClosingHashes(ln.slice(head[0].length));
      blocks.push({ t: "heading", level: head[1].length, c: parseInline(content, 0) });
      i++;
      continue;
    }

    if (HR.test(ln)) { blocks.push({ t: "hr" }); i++; continue; }

    if (QUOTE.test(ln)) {
      const inner = [];
      while (i < n && QUOTE.test(lines[i])) { inner.push(lines[i].replace(QUOTE, "")); i++; }
      blocks.push({ t: "quote", c: parseBlocks(inner, depth + 1) });
      continue;
    }

    if (LIST.test(ln)) {
      const res = parseList(lines, i, depth);
      blocks.push(res.block);
      i = res.next;
      continue;
    }

    if (tableStartsAt(lines, i)) {
      const headCells = splitRow(lines[i]);
      const rows = [];
      i += 2;
      while (i < n && !isBlank(lines[i]) && lines[i].includes("|")) { rows.push(splitRow(lines[i])); i++; }
      const width = Math.max(headCells.length, ...rows.map((r) => r.length));
      const pad = (cells) => Array.from({ length: width }, (_, k) => parseInline(cells[k] || "", 0));
      blocks.push({ t: "table", head: pad(headCells), rows: rows.map(pad) });
      continue;
    }

    const para = [ln];
    i++;
    while (i < n && !isBlank(lines[i]) && !startsBlock(lines, i)) { para.push(lines[i]); i++; }
    blocks.push({ t: "p", c: inlineLines(para) });
  }
  return blocks;
}

function parseList(lines, start, depth) {
  const n = lines.length;
  const first = LIST.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2][0]);
  const items = [];
  const startNo = ordered ? parseInt(first[2], 10) : 1;
  let i = start;
  while (i < n) {
    const m = LIST.exec(lines[i]);
    if (!m || m[1].length < base || /\d/.test(m[2][0]) !== ordered || HR.test(lines[i])) break;
    const spaces = m[0].length - m[1].length - m[2].length;
    const hasText = m[0].length < lines[i].length;
    const offset = m[1].length + m[2].length + (spaces >= 1 && spaces <= 4 && hasText ? spaces : 1);
    const body = [lines[i].slice(offset)];
    i++;
    while (i < n) {
      const ln = lines[i];
      if (isBlank(ln)) {
        let k = i;
        while (k < n && isBlank(lines[k])) k++;
        if (k < n && indentOf(lines[k]) >= offset) {
          for (; i < k; i++) body.push("");
          continue;
        }
        break;
      }
      if (indentOf(ln) >= offset) { body.push(ln.slice(offset)); i++; continue; }
      if (!isBlank(lines[i - 1]) && !startsBlock(lines, i)) { body.push(ln.trimStart()); i++; continue; }
      break;
    }
    items.push(parseBlocks(body, depth + 1));
    let j = i;
    while (j < n && isBlank(lines[j])) j++;
    if (j < n && j > i && LIST.test(lines[j])) i = j;
  }
  return { block: { t: ordered ? "ol" : "ul", start: startNo, items }, next: i };
}

// ---- inline parsing -----------------------------------------------------------------------

function destinationOf(rawHref) {
  let href = rawHref.trim();
  const ws = href.search(/\s/);
  if (ws > 0) href = href.slice(0, ws);       // drop an optional "title"
  if (href.startsWith("<") && href.endsWith(">")) href = href.slice(1, -1);
  return href;
}

function classifyLink(href, children) {
  const video = HAS_SCHEME.test(href) ? null : VIDEO_MD.exec(href);
  if (video) {
    let id = video[1];
    try { id = decodeURIComponent(id); } catch { /* keep the raw id */ }
    return { t: "ref", id, c: children };
  }
  try {
    const url = new URL(href);
    if (url.protocol === "https:" && YOUTUBE_HOSTS.has(url.hostname)) return { t: "link", href: url.href, c: children };
  } catch { /* relative or malformed: label only */ }
  return null;
}

const NOT_A_LINK = -2;

// Index of the ")" that closes a link destination (balanced inner parens); -1 if none exists in the rest of the
// text (cacheable); NOT_A_LINK if a "[" shows up first (this opener only, not cacheable).
function closingParen(s, from) {
  let level = 0;
  for (let k = from; k < s.length; k++) {
    const c = s[k];
    if (c === "[") return NOT_A_LINK;
    if (c === "(") level++;
    else if (c === ")") {
      if (level === 0) return k;
      level--;
    }
  }
  return -1;
}

function parseInline(s, depth) {
  const out = [];
  const n = s.length;
  let buf = "";
  let i = 0;
  // "no closing delimiter exists after here" caches keep unclosed markers linear
  const noClose = { strong: false, strong3: false, em: false, us: false, paren: false, link: false };
  const noCode = new Set();
  let linkClose = -1;
  const flush = () => { if (buf) { out.push({ t: "text", v: buf }); buf = ""; } };
  const nested = (text) => (depth + 1 > MAX_INLINE_DEPTH ? [{ t: "text", v: text }] : parseInline(text, depth + 1));
  const isSpace = (c) => c === undefined || c === " " || c === "\t";
  const isWord = (c) => c !== undefined && WORD_CHAR.test(c);

  while (i < n) {
    const ch = s[i];

    if (ch === "\\" && i + 1 < n && ESCAPABLE.includes(s[i + 1])) { buf += s[i + 1]; i += 2; continue; }

    if (ch === "`") {
      let run = 1;
      while (s[i + run] === "`") run++;
      let close = -1;
      if (!noCode.has(run)) {
        let from = i + run;
        while (from < n) {
          const at = s.indexOf("`", from);
          if (at < 0) break;
          let len = 1;
          while (s[at + len] === "`") len++;
          if (len === run) { close = at; break; }
          from = at + len;
        }
        if (close < 0) noCode.add(run);
      }
      if (close < 0) { buf += "`".repeat(run); i += run; continue; }
      let code = s.slice(i + run, close);
      if (code.length > 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim() !== "") code = code.slice(1, -1);
      flush();
      out.push({ t: "code", v: code });
      i = close + run;
      continue;
    }

    if (ch === "*") {
      let run = 1;
      while (s[i + run] === "*") run++;
      const canOpen = depth < MAX_INLINE_DEPTH && !isSpace(s[i + Math.min(run, 3)]);
      if (canOpen && run >= 2) {
        const width = run >= 3 ? 3 : 2;
        const marker = "*".repeat(width);
        let close = -1;
        const cacheKey = width === 3 ? "strong3" : "strong";
        if (!noClose[cacheKey]) {
          let from = i + width;
          for (;;) {
            const at = s.indexOf(marker, from);
            if (at < 0) { noClose[cacheKey] = true; break; }
            if (at > i + width && !isSpace(s[at - 1])) { close = at; break; }
            from = at + 1;
          }
        }
        if (close > 0) {
          flush();
          const inner = nested(s.slice(i + width, close));
          out.push({ t: "strong", c: width === 3 ? [{ t: "em", c: inner }] : inner });
          i = close + width;
          continue;
        }
      } else if (canOpen) {
        let close = -1;
        if (!noClose.em) {
          let from = i + 1;
          for (;;) {
            const at = s.indexOf("*", from);
            if (at < 0) { noClose.em = true; break; }
            if (s[at + 1] === "*" || s[at - 1] === "*") {
              from = at + 1;
              while (s[from] === "*") from++;
              continue;
            }
            if (at > i + 1 && !isSpace(s[at - 1])) { close = at; break; }
            from = at + 1;
          }
        }
        if (close > 0) {
          flush();
          out.push({ t: "em", c: nested(s.slice(i + 1, close)) });
          i = close + 1;
          continue;
        }
      }
      buf += "*".repeat(run);
      i += run;
      continue;
    }

    if (ch === "_" && depth < MAX_INLINE_DEPTH && !noClose.us && s[i + 1] !== "_" && s[i - 1] !== "_" &&
        !isWord(s[i - 1]) && !isSpace(s[i + 1])) {
      let close = -1;
      let from = i + 1;
      for (;;) {
        const at = s.indexOf("_", from);
        if (at < 0) { noClose.us = true; break; }
        if (at > i + 1 && s[at - 1] !== "_" && s[at + 1] !== "_" && !isSpace(s[at - 1]) && !isWord(s[at + 1])) {
          close = at;
          break;
        }
        from = at + 1;
      }
      if (close > 0) {
        flush();
        out.push({ t: "em", c: nested(s.slice(i + 1, close)) });
        i = close + 1;
        continue;
      }
    }

    if (ch === "[" && !noClose.link && !noClose.paren) {
      if (linkClose < i) {
        linkClose = s.indexOf("](", i + 1);
        if (linkClose < 0) noClose.link = true;
      }
      if (!noClose.link) {
        const nextOpen = s.indexOf("[", i + 1);
        if (nextOpen < 0 || nextOpen > linkClose) {
          const end = closingParen(s, linkClose + 2);
          if (end === -1) noClose.paren = true;
          else if (end !== NOT_A_LINK) {
            const href = destinationOf(s.slice(linkClose + 2, end));
            const label = nested(s.slice(i + 1, linkClose));
            const node = classifyLink(href, label);
            flush();
            if (node) out.push(node);
            else out.push(...label, { t: "text", v: ` (${href})` });   // keep the target visible, but not clickable
            i = end + 1;
            continue;
          }
        }
      }
    }

    buf += ch;
    i++;
  }
  flush();
  return out;
}

// ---- rendering ----------------------------------------------------------------------------

const defaultRefHref = (id) => "#/refs/" + encodeURIComponent(id);

export function renderMarkdown(text, opts = {}) {
  const refHref = typeof opts.refHref === "function" ? opts.refHref : defaultRefHref;
  const frag = document.createDocumentFragment();
  const blocks = parseMarkdown(text);
  const first = blocks[0];
  // opts.dropTitle: skip a leading "# <title>" that only repeats the page heading
  if (typeof opts.dropTitle === "string" && first && first.t === "heading" && first.level === 1 &&
      plainText(first.c).trim() === opts.dropTitle.trim()) blocks.shift();
  for (const block of blocks) frag.append(renderBlock(block, refHref));
  return frag;
}

function plainText(nodes) {
  return nodes.map((n) => (n.t === "text" || n.t === "code" ? n.v : n.t === "br" ? " " : plainText(n.c))).join("");
}

function renderInline(nodes, refHref) {
  return nodes.map((node) => {
    switch (node.t) {
      case "text": return node.v;
      case "br": return h("br");
      case "code": return h("code", {}, node.v);
      case "strong": return h("strong", {}, ...renderInline(node.c, refHref));
      case "em": return h("em", {}, ...renderInline(node.c, refHref));
      case "ref": return h("a", { href: refHref(node.id) }, ...renderInline(node.c, refHref));
      case "link":
        return h("a", { href: node.href, target: "_blank", rel: "noopener noreferrer" }, ...renderInline(node.c, refHref));
      default: return "";
    }
  });
}

function renderBlock(block, refHref) {
  switch (block.t) {
    case "heading":
      return h("h" + Math.min(6, Math.max(3, block.level)), {}, ...renderInline(block.c, refHref));
    case "p":
      return h("p", {}, ...renderInline(block.c, refHref));
    case "ul":
    case "ol": {
      const attrs = block.t === "ol" && block.start !== 1 ? { start: String(block.start) } : {};
      return h(block.t, attrs, ...block.items.map((item) => renderItem(item, refHref)));
    }
    case "quote":
      return h("blockquote", {}, ...block.c.map((b) => renderBlock(b, refHref)));
    case "code":
      return h("pre", { tabindex: "0", "aria-label": "코드 블록" }, h("code", {}, block.v));
    case "hr":
      return h("hr");
    case "table":
      return h("div", { class: "md-table-wrap", tabindex: "0", role: "region", "aria-label": "표" },
        h("table", { class: "md-table" },
          h("thead", {}, h("tr", {}, ...block.head.map((c) => h("th", { scope: "col" }, ...renderInline(c, refHref))))),
          h("tbody", {}, ...block.rows.map((row) =>
            h("tr", {}, ...row.map((c) => h("td", {}, ...renderInline(c, refHref))))))));
    default:
      return h("div");
  }
}

// Tight list items: a leading paragraph is inlined instead of wrapped in <p>.
function renderItem(blocks, refHref) {
  const kids = [];
  blocks.forEach((b, idx) => {
    if (idx === 0 && b.t === "p") kids.push(...renderInline(b.c, refHref));
    else kids.push(renderBlock(b, refHref));
  });
  return h("li", {}, ...kids);
}
