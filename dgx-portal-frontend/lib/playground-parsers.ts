/* Playground model-output parsers — pure helpers extracted from
 * app/(app)/playground/page.tsx (a MOVE, not a rewrite: the code and comments
 * are unchanged) so they can be unit-tested: they encode the model→UI
 * contract (what the model may emit and how the UI copes with it).
 *
 * Everything here is pure: no React, no fetch, no DOM. Keep it that way. */

// The model asks as many questions as it sees fit — two or ten. These
// limits are NOT editorial framing but a safety net: a generation
// that goes off the rails must not produce an endless questionnaire.
export const MAX_ASK_QUESTIONS = 20;
export const MAX_ASK_OPTIONS = 8;

/** Body of a questions block, whatever the fence label.
 *  Seen in prod on 2026-10-01 (MiMo): the model puts its questions in a
 *  ```json block instead of ```ask. The questionnaire then came out as « fichier-2.json »
 *  in the panel, and the user had nothing to click. We therefore accept
 *  ```json or a language-less fence, but ONLY if the body starts with
 *  {"questions": [ — a real JSON file has no reason to start that way. */
export const RE_QUESTIONS_BODY = /^\s*\{\s*"questions"\s*:\s*\[/;
export function isQuestionsBlock(lang: string, body: string): boolean {
  const l = lang.toLowerCase();
  return l === "ask" || ((l === "json" || l === "") && RE_QUESTIONS_BODY.test(body));
}

// One clarifying question + its proposed answers.
export type AskQ = { question: string; options: string[] };
// A model's clarifying block: one or more questions, plus any prose around it.
export type AskBlock = { questions: AskQ[]; prose: string };

/** A targeted edit to an already produced file. */
export type FileEdit = { file: string; find: string; replace: string };

/* ── Compatibility: old conversations ────────────────────────────────────────
 * The model no longer receives the edit protocol (it rewrites the file
 * entirely). These functions remain because the already saved history
 * contains ```edit blocks: without them, those conversations would show
 * raw JSON instead of the fixed file. Nothing new produces them.
 */

/** Reads a ```edit block. Same tolerance as parseAsk: these blocks come from
 *  a model, they sometimes arrive truncated or followed by junk. */
export function parseEdits(content: string): FileEdit[] {
  const m = content.match(/```edit\s*\n([\s\S]*?)(?:```|$)/);
  if (!m) return [];
  try {
    const body = m[1].trim();
    let obj;
    try {
      obj = JSON.parse(body);
    } catch {
      // Three defects seen in the wild, in order of frequency: raw line
      // breaks inside a string, junk after the end, unclosed object.
      const repaired = escapeRawControlChars(body);
      try {
        obj = JSON.parse(repaired);
      } catch {
        try {
          obj = JSON.parse(firstJsonValue(repaired) ?? balanceJson(repaired));
        } catch {
          // mismatched closers (« } » instead of « ] »)
          obj = JSON.parse(repairClosings(repaired));
        }
      }
    }
    const raw: unknown[] = Array.isArray(obj.edits) ? obj.edits : (obj.find ? [obj] : []);
    return raw
      .map((e) => {
        const ee = e as { file?: unknown; find?: unknown; replace?: unknown };
        return {
          file: typeof ee.file === "string" ? ee.file.trim() : "",
          find: typeof ee.find === "string" ? ee.find : "",
          replace: typeof ee.replace === "string" ? ee.replace : "",
        };
      })
      .filter((e) => e.find !== "");
  } catch {
    return [];
  }
}

/** Repairs MISMATCHED closers (« } » instead of « ] », extra
 *  closer), following the stack of openers outside strings.
 *  Seen in prod on 2026-10-01 (MiMo): `"options": ["a", "b"}]}]}` — the
 *  options list closed by a brace. Neither the cut (firstJsonValue) nor the
 *  rebalancing (balanceJson) could cope, and the whole questionnaire
 *  displayed as raw JSON instead of the question card.
 *  Rule: a closer that does not match the top first closes what is
 *  actually open; a closer without an opener is ignored; what
 *  is still open at the end is closed. */
export function repairClosings(src: string): string {
  const stack: string[] = [];
  let out = "";
  let inString = false;
  let escaped = false;
  for (const ch of src) {
    if (inString) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; out += ch; continue; }
    if (ch === "{" || ch === "[") { stack.push(ch === "{" ? "}" : "]"); out += ch; continue; }
    if (ch === "}" || ch === "]") {
      if (!stack.includes(ch)) continue;                    // orphan closer
      while (stack.length && stack[stack.length - 1] !== ch) out += stack.pop();
      out += stack.pop();
      continue;
    }
    out += ch;
  }
  if (inString) out += '"';
  // Dangling comma before a closer (« "b",] »): invalid in JSON.
  return (out + stack.reverse().join("")).replace(/,\s*([}\]])/g, "$1");
}

// Closes a JSON truncated at the end of a string. A model this size
// regularly forgets the last `}` or `]` — a single missing character made
// JSON.parse fail, and the questions block fell back to raw JSON before the
// user's eyes (seen in production). We rebalance rather than give up.
export function balanceJson(src: string): string {
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (const ch of src) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{" || ch === "[") stack.push(ch);
    else if (ch === "}" || ch === "]") stack.pop();
  }
  let out = src.replace(/,\s*$/, "");     // trailing comma before the cut
  if (inString) out += '"';                // string left open
  while (stack.length) out += stack.pop() === "{" ? "}" : "]";
  return out;
}

/** The message text, with the never-closed fence force-closed.
 *
 * `parseArtifacts` only extracts a block DELIMITED on both sides. An answer
 * cut mid-file therefore produced none: no card, no preview, no
 * download — the code poured as is into the bubble. We close the
 * block to salvage what was written; it is always better than nothing, and the
 * « Réponse coupée » banner says it is not finished.
 */
export function closedContent(content: string): string {
  const fences = content.match(/```/g);
  return fences && fences.length % 2 === 1 ? content + "\n```" : content;
}

/** What must be glued back onto the file: the resume message, without its wrapper.
 *
 * A resume starts IN THE MIDDLE of the block. Three forms seen in the wild:
 *  - the model reopens a fence: we take the block body;
 *  - it continues with the raw content then CLOSES the block: the only ``` is a
 *    closer, so the body is what PRECEDES it (reading it backwards only
 *    yielded the concluding sentence, and the file stayed truncated);
 *  - there is no fence at all: the whole message is content.
 */
export function continuationBody(content: string): string {
  const closed = content.match(/```[^\n`]*\n([\s\S]*?)```/);
  if (closed) return closed[1].replace(/\n$/, "");
  const first = content.indexOf("```");
  if (first < 0) return content;
  // An OPENING fence is followed by an info-string then a line break, and
  // sits at the top of the message; otherwise it is a closing fence.
  const opening = /^\s*```[^\n`]*\n/.test(content);
  if (opening) return openCodeFence(content)?.body ?? content;
  return content.slice(0, first).replace(/\n$/, "");
}

/** Glues a continuation onto an unfinished file, absorbing what it repeats.
 *
 * The model does not resume character for character: it patches up the cut word
 * then re-emits the current block from its start. Naively gluing duplicated the
 * code and left an extra brace — the page displayed but its script died
 * on « Unexpected end of input », so no chessboard. We look for the longest
 * run of lines common to the END of the file and the START of the resume, and
 * splice there. The search is bounded to the tail of the file: even when
 * fooled, it can never amputate the beginning.
 */
const REJOIN_TAIL = 6000;      // portion of the file end where we search
const REJOIN_HEAD  = 6000;      // portion of the resume start where we search
const REJOIN_MIN_LINES = 3;    // below that, it is noise (« } », empty lines)
const REJOIN_MIN_CHARS = 60;
const REJOIN_MAX_REMOVED = 2000;   // beyond that, we prefer duplicating to losing

export function splitLines(text: string, startPos: number) {
  const lines: { text: string; pos: number }[] = [];
  let pos = startPos;
  for (const l of text.split("\n")) {
    lines.push({ text: l.trimEnd(), pos });
    pos += l.length + 1;
  }
  return lines;
}

export function rejoin(base: string, continuation: string): string {
  // A continuation that REALLY rewrites the whole document is a replacement, not an
  // addition. We require it to go all the way and to pull its weight: without
  // these two conditions, a three-line resume starting with « <html> »
  // wiped out a file of several thousand lines.
  if (/^\s*(<!DOCTYPE|<html[\s>])/i.test(continuation) && /<!DOCTYPE|<html[\s>]/i.test(base)
      && /<\/html\s*>/i.test(continuation) && continuation.length >= base.length * 0.5) {
    return continuation;
  }
  const queue = base.slice(-REJOIN_TAIL);
  const lb = splitLines(queue, base.length - queue.length);
  const ls = splitLines(continuation.slice(0, REJOIN_HEAD), 0);
  let best: { base: number; continuation: number; charCount: number } | null = null;
  for (let i = 0; i < lb.length; i++) {
    for (let j = 0; j < ls.length; j++) {
      let k = 0, charCount = 0, useful = 0;
      while (i + k < lb.length && j + k < ls.length && lb[i + k].text === ls[j + k].text) {
        const t = lb[i + k].text.trim();
        charCount += t.length;
        if (t.length >= 10) useful++;      // « } » or an empty line proves nothing
        k++;
      }
      if (k >= REJOIN_MIN_LINES && useful >= 1 && charCount >= REJOIN_MIN_CHARS
          && (!best || charCount > best.charCount)) {
        best = { base: lb[i].pos, continuation: ls[j].pos, charCount };
      }
    }
  }
  // Safety net: splicing DELETES the file's tail. The real overlap
  // observed is a few hundred characters (the model re-emits the current block);
  // beyond that, a chance match would destroy good code. When in
  // doubt we glue end to end: a bit of duplication is visible and fixable,
  // vanished code is not.
  if (best && base.length - best.base <= REJOIN_MAX_REMOVED) {
    return base.slice(0, best.base) + continuation.slice(best.continuation);
  }
  return joinLine(base, continuation);
}

/** Splices a continuation that REPEATS the end of the cut line.
 *
 * The token ceiling lands mid-line, and the model often resumes
 * the word (or line) it had stopped at: measured on 2026-10-01 on MiMo,
 * « p_del » + « del = sub.add_parser(…) » gave « p_deldel », NameError at
 * launch. Line splicing sees nothing (fewer than 3 common lines).
 * We look for the longest start of the continuation that already ends the cut
 * line; at least 3 characters, so as not to trim a legitimate character by chance. */
function joinLine(base: string, continuation: string): string {
  const end = base.slice(base.lastIndexOf("\n") + 1);
  if (!end.trim()) return base + continuation;
  const head = continuation.split("\n", 1)[0];
  for (let k = Math.min(end.length, head.length); k >= 3; k--) {
    if (end.endsWith(head.slice(0, k))) return base + continuation.slice(k);
  }
  return base + continuation;
}

/** Escapes control characters left RAW in a JSON string.
 *
 * A model writing code in a « replace » field regularly forgets
 * to escape its line breaks: the string contains a real newline,
 * which JSON forbids, and the whole block becomes unreadable. Seen in
 * production on an edit block of several dozen lines.
 */
export function escapeRawControlChars(src: string): string {
  let out = "";
  let inString = false;
  let escaped = false;
  for (const ch of src) {
    if (inString) {
      if (escaped) { escaped = false; out += ch; continue; }
      if (ch === "\\") { escaped = true; out += ch; continue; }
      if (ch === '"') { inString = false; out += ch; continue; }
      if (ch === "\n") { out += "\\n"; continue; }
      if (ch === "\r") { out += "\\r"; continue; }
      if (ch === "\t") { out += "\\t"; continue; }
      out += ch;
      continue;
    }
    if (ch === '"') inString = true;
    out += ch;
  }
  return out;
}

/** The first complete JSON value of the string, ignoring what follows.
 *
 * The model sometimes adds a character AFTER the closed object (seen: an
 * orphan quote, « …]}\" »). JSON.parse rejects anything following a complete
 * value, and rebalancing is useless here: the JSON is not truncated,
 * it is followed by junk. So we cut as soon as the depth returns to zero.
 */
export function firstJsonValue(src: string): string | null {
  const start = src.search(/[{[]/);
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{" || ch === "[") depth++;
    else if (ch === "}" || ch === "]") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  return null;   // never closed → it is truncation, balanceJson handles it
}

// ── Clarification questionnaire ─────────────────────────────────────────────
// The format asked of the model is ONE well-formed ```ask block. In practice
// it misses it sometimes: an empty `{"questions": []}` envelope closed right
// away, then the real questions scattered in the text as separate
// `{"question": …, "options": […]}` objects, interleaved with debris (measured
// 2026-10-05: the questionnaire came out as raw text, nothing to click).
// The tolerance therefore lives in THIS parser, never in the instruction to
// the model: we pick up every object that BEGINS like a question, wherever
// it is.

/** Position right after the object opened at `debut`, or null if it never
 *  closes (truncation). Strings are read: a brace inside a text does not
 *  close the object by accident. */
function objectEnd(src: string, start: number): number | null {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return null;
}

/** Every object that BEGINS like a questionnaire entry, with its position.
 *
 * We look for the START (`{"question"` / `{"questions"`) rather than a
 * well-formed block: this is what survives a model that scatters its output
 * and recovers. An object can hide another one (`{"questions": [{…}]`): we
 * rescan right after the opening brace. */
function questionCandidates(src: string): { text: string; start: number; end: number }[] {
  const out: { text: string; start: number; end: number }[] = [];
  const rx = /\{\s*"(?:question|questions)"\s*:/gi;
  let m: RegExpExecArray | null;
  while ((m = rx.exec(src))) {
    const start = m.index;
    const end = objectEnd(src, start);
    out.push({ text: src.slice(start, end ?? src.length), start, end: end ?? src.length });
    rx.lastIndex = start + 1;
  }
  return out;
}

/** The questions carried by a JSON text, through the repair chain.
 *
 * Three defects seen in production, in order: raw line breaks inside a
 * string, junk after the object, object never closed. What remains
 * unrecoverable yields ZERO questions — other candidates will carry the
 * others. */
function questionsOf(body: string): AskQ[] {
  let obj: unknown;
  try {
    obj = JSON.parse(body);
  } catch {
    const repaired = escapeRawControlChars(body);
    try {
      obj = JSON.parse(repaired);
    } catch {
      try {
        obj = JSON.parse(firstJsonValue(repaired) ?? balanceJson(repaired));
      } catch {
        try {
          obj = JSON.parse(repairClosings(repaired));
        } catch {
          return [];
        }
      }
    }
  }
  const raw = obj as { question?: unknown; questions?: unknown } | null;
  const list: unknown[] = Array.isArray(raw?.questions) ? raw.questions : raw?.question ? [raw] : [];
  return list
    .map((q) => {
      const qq = q as { question?: unknown; options?: unknown };
      const question = typeof qq.question === "string" ? qq.question.trim() : "";
      const options = Array.isArray(qq.options)
        ? qq.options.filter((o: unknown) => typeof o === "string" && o.trim()).map((o: string) => o.trim()).slice(0, MAX_ASK_OPTIONS)
        : [];
      return { question, options };
    })
    .filter((q) => q.question && q.options.length >= 1);
}

/** Bounds [debut, fin) of the code blocks that are real FILES: a file must
 *  not be eaten because it contains question objects (a FAQ in JSON, a
 *  quiz…). The criterion is EXACTLY the one of the file rendering
 *  (`isQuestionsBlock`): a block that can carry a questionnaire does not
 *  protect its contents, all the others do. */
function fileBounds(content: string): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (const m of content.matchAll(/```([\w+-]*)[ \t]*\n([\s\S]*?)(?:```|$)/g)) {
    if (isQuestionsBlock(m[1] || "", m[2])) continue;
    // A block WITHOUT a language and NEVER closed is not a file: it is the
    // orphan marker of a failed block (« {"questions": []}``` » measured on
    // 2026-10-05) — protecting it would have hidden every question after it.
    if (!m[1] && !m[0].endsWith("```")) continue;
    out.push([m.index ?? 0, (m.index ?? 0) + m[0].length]);
  }
  return out;
}

/** Does the message carry questionnaire objects outside the real files?
 *
 * This is what distinguishes a FAILED questionnaire from an open file: the
 * model that scatters its questions sometimes leaves an orphan « ``` »
 * marker, and `openCodeFence` then saw an unclosed « text » file — the
 * automatic resume relaunched the model (« reprends au caractère suivant »),
 * which REWROTE its questionnaire: the two attempts tangled and the
 * questions came out as raw text (measured on 2026-10-05). */
export function containsQuestions(content: string): boolean {
  const files = fileBounds(content);
  return questionCandidates(content).some(
    (c) => !files.some(([d, f]) => c.start >= d && c.end <= f),
  );
}

/** Does this answer leave an UNFINISHED file?
 *
 * A questionnaire is not a file: never « resume » it. An HTML file that
 * does end with `</html>` is not unfinished either (the model merely
 * forgot to close its block). */
export function incompleteResponse(content: string): boolean {
  if (containsQuestions(content)) return false;
  const open = openCodeFence(content);
  if (open) return !/<\/html\s*>\s*$/i.test(open.body.trimEnd());
  if (/<!DOCTYPE html|<html[\s>]/i.test(content) && !/<\/html\s*>/i.test(content)) return true;
  return false;
}

/** Detects a clarification questionnaire wherever the model scattered it.
 *
 * Accepts the ```ask block, the ```json behind the `{"questions": [` guard,
 * the degenerate « ask » + bare JSON form (MiMo, 2026-10-02), and — since
 * 2026-10-05 — the question objects SET IN THE TEXT after a failed block. A
 * forgotten closing fence is not a showstopper either. */
export function parseAsk(content: string): AskBlock | null {
  const files = fileBounds(content);
  const questions: AskQ[] = [];
  const seen = new Set<string>();
  let first = content.length;   // début du premier fragment de question

  const collected = (body: string, offset: number) => {
    for (const c of questionCandidates(body)) {
      const start = offset + c.start;
      const end = offset + c.end;
      if (files.some(([d, f]) => start >= d && end <= f)) continue;   // it is a
                                                                      // file
      first = Math.min(first, start);
      for (const q of questionsOf(c.text)) {
        // Deduplication key, never displayed: deliberately locale-free.
        const key = q.question.toLowerCase();
        if (seen.has(key) || questions.length >= MAX_ASK_QUESTIONS) continue;
        seen.add(key);
        questions.push(q);
      }
    }
  };

  // 1) The blocks we ASK it to write (we keep their place for the prose).
  for (const m of content.matchAll(/```ask[ \t]*\n([\s\S]*?)(?:```|$)/gi)) {
    first = Math.min(first, m.index ?? 0);
    collected(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  for (const m of content.matchAll(/```(?:json)?[ \t]*\n(\s*\{\s*"questions"\s*:\s*\[[\s\S]*?)(?:```|$)/g)) {
    first = Math.min(first, m.index ?? 0);
    collected(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  // Degenerate form measured on MiMo on 2026-10-02: « ask » in inline code (or
  // bare) followed by the JSON WITHOUT a closing fence. The `{"questions": [
  // guard avoids swallowing a real file along the way.
  for (const m of content.matchAll(/(?:^|\n)[ \t]*`?ask`?[ \t]*\n(\s*\{\s*"questions"\s*:\s*\[[\s\S]*?)(?:```|$)/gi)) {
    first = Math.min(first, m.index ?? 0);
    collected(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  // 2) And above all: the question objects SET IN THE TEXT, wherever they
  //    are — this is what remains when the opening block failed.
  collected(content, 0);

  if (!questions.length) return null;
  // The questionnaire prose = what PRECEDES the first question fragment. The
  // instruction allows only ONE intro sentence before the block; we cut the
  // JSON debris there (bracket/brace followed by a quote, « ``` » marker): a
  // model in difficulty mixes its intro with its questions.
  let prose = content.slice(0, first);
  const debris = prose.search(/(?:[[{]\s*"|```)/);
  if (debris >= 0) prose = prose.slice(0, debris);
  return { questions, prose: prose.trim() };
}

/** The code block still open at the end of the content, if any.
 *
 * During the stream, this is the file the model is writing. We
 * route it to the side panel instead of letting it scroll in the chat and then
 * moving it all at once at the end — which gave the impression that
 * files were "copied over" twice.
 */
export function openCodeFence(content: string): { lang: string; body: string; start: number } | null {
  const fences = content.match(/```/g);
  if (!fences || fences.length % 2 === 0) return null;   // everything is closed
  const start = content.lastIndexOf("```");
  const rest = content.slice(start + 3);
  const nl = rest.indexOf("\n");
  if (nl < 0) return null;                               // the info-string is not finished
  const info = rest.slice(0, nl).trim();
  const first = info.split(/\s+/)[0] || "";
  if (isQuestionsBlock(first, rest.slice(nl + 1))) return null;   // handled by parseAsk
  return { lang: first || "text", body: rest.slice(nl + 1), start };
}

// Extensions recognized as FILES. Deliberately a closed list: without
// it, « ansible.builtin.reboot » or « os_family['debian'] » would pass for
// file names and produce absurd titles.
export const FILE_EXT = new RegExp(
  "\\.(ya?ml|json|jsonc|toml|ini|cfg|conf|env|py|js|mjs|cjs|ts|tsx|jsx|sh|bash|zsh|" +
  "go|rs|rb|php|java|kt|c|h|cpp|sql|html|css|scss|md|txt|log|xml|service|tf|gradle)$",
  "i");
export const BARE_FILES = /^(Dockerfile|Makefile|Vagrantfile|Jenkinsfile|Procfile)$/i;

/** File names the user's request spells out (« un script hello.py »). */
export function askedFileNames(request: string): string[] {
  const names: string[] = [];
  for (const m of (request || "").matchAll(/(?:^|[\s(`"'«])([\w.-]+(?:\/[\w.-]+)*)(?=[\s:,;)`"'».!?]|$)/g)) {
    const c = m[1].replace(/^[.\/]+/, "").replace(/\.$/, "");
    if ((FILE_EXT.test(c) || BARE_FILES.test(c)) && !names.includes(c)) names.push(c);
  }
  return names;
}

/** Is a block that prose names after a file only an EXCERPT of that file?
 *
 * « Remplace dans garage.toml : » + two lines: the name says WHERE the lines
 * go, not that they are the whole file. Taken for a file, the excerpt left the
 * explanation (« Remplace dans garage.toml : » followed by nothing) and became
 * the latest garage.toml — the next edit worked on two lines (2026-10-12).
 *
 * Only a file that ALREADY exists can have an excerpt (`existing` = its
 * length): « mets ceci dans docker-compose.yml : » before any compose is the
 * whole file. `lead` is the prose just before the block. */
export function isExcerptOf(title: string, body: string, lead: string, existing = 0): boolean {
  if (existing <= 0 || body.split("\n").length > 15) return false;
  if (body.length * 3 < existing) return true;
  const name = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // The name right after a « where » preposition, possibly behind « le
  // fichier » / « the file » and backticks.
  const where = new RegExp(
    "(?:^|[\\s(])(?:dans|à|au|sous|in|into|under)\\s+(?:(?:le|la|ton|ta|votre|the|your)\\s+)?"
      + "(?:fichier\\s+|file\\s+)?[`*\"']*" + name + "[`*\"']*",
    "i");
  return where.test(lead.slice(-200));
}
