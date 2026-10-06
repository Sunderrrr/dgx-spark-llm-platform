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
export const RE_CORPS_QUESTIONS = /^\s*\{\s*"questions"\s*:\s*\[/;
export function estBlocQuestions(lang: string, corps: string): boolean {
  const l = lang.toLowerCase();
  return l === "ask" || ((l === "json" || l === "") && RE_CORPS_QUESTIONS.test(corps));
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
      const repare = escapeRawControlChars(body);
      try {
        obj = JSON.parse(repare);
      } catch {
        try {
          obj = JSON.parse(firstJsonValue(repare) ?? balanceJson(repare));
        } catch {
          // mismatched closers (« } » instead of « ] »)
          obj = JSON.parse(reparerFermetures(repare));
        }
      }
    }
    const brut: unknown[] = Array.isArray(obj.edits) ? obj.edits : (obj.find ? [obj] : []);
    return brut
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
export function reparerFermetures(src: string): string {
  const pile: string[] = [];
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
    if (ch === "{" || ch === "[") { pile.push(ch === "{" ? "}" : "]"); out += ch; continue; }
    if (ch === "}" || ch === "]") {
      if (!pile.includes(ch)) continue;                    // orphan closer
      while (pile.length && pile[pile.length - 1] !== ch) out += pile.pop();
      out += pile.pop();
      continue;
    }
    out += ch;
  }
  if (inString) out += '"';
  // Dangling comma before a closer (« "b",] »): invalid in JSON.
  return (out + pile.reverse().join("")).replace(/,\s*([}\]])/g, "$1");
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
export function contenuCloture(content: string): string {
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
export function corpsDeSuite(content: string): string {
  const ferme = content.match(/```[^\n`]*\n([\s\S]*?)```/);
  if (ferme) return ferme[1].replace(/\n$/, "");
  const premier = content.indexOf("```");
  if (premier < 0) return content;
  // An OPENING fence is followed by an info-string then a line break, and
  // sits at the top of the message; otherwise it is a closing fence.
  const ouvrante = /^\s*```[^\n`]*\n/.test(content);
  if (ouvrante) return openCodeFence(content)?.body ?? content;
  return content.slice(0, premier).replace(/\n$/, "");
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
const RECOL_QUEUE = 6000;      // portion of the file end where we search
const RECOL_TETE  = 6000;      // portion of the resume start where we search
const RECOL_MIN_LIGNES = 3;    // below that, it is noise (« } », empty lines)
const RECOL_MIN_CARS = 60;
const RECOL_MAX_SUPPRIME = 2000;   // beyond that, we prefer duplicating to losing

export function decoupeLignes(texte: string, depart: number) {
  const lignes: { texte: string; pos: number }[] = [];
  let pos = depart;
  for (const l of texte.split("\n")) {
    lignes.push({ texte: l.trimEnd(), pos });
    pos += l.length + 1;
  }
  return lignes;
}

export function recoller(base: string, suite: string): string {
  // A continuation that REALLY rewrites the whole document is a replacement, not an
  // addition. We require it to go all the way and to pull its weight: without
  // these two conditions, a three-line resume starting with « <html> »
  // wiped out a file of several thousand lines.
  if (/^\s*(<!DOCTYPE|<html[\s>])/i.test(suite) && /<!DOCTYPE|<html[\s>]/i.test(base)
      && /<\/html\s*>/i.test(suite) && suite.length >= base.length * 0.5) {
    return suite;
  }
  const queue = base.slice(-RECOL_QUEUE);
  const lb = decoupeLignes(queue, base.length - queue.length);
  const ls = decoupeLignes(suite.slice(0, RECOL_TETE), 0);
  let meilleur: { base: number; suite: number; cars: number } | null = null;
  for (let i = 0; i < lb.length; i++) {
    for (let j = 0; j < ls.length; j++) {
      let k = 0, cars = 0, utiles = 0;
      while (i + k < lb.length && j + k < ls.length && lb[i + k].texte === ls[j + k].texte) {
        const t = lb[i + k].texte.trim();
        cars += t.length;
        if (t.length >= 10) utiles++;      // « } » or an empty line proves nothing
        k++;
      }
      if (k >= RECOL_MIN_LIGNES && utiles >= 1 && cars >= RECOL_MIN_CARS
          && (!meilleur || cars > meilleur.cars)) {
        meilleur = { base: lb[i].pos, suite: ls[j].pos, cars };
      }
    }
  }
  // Safety net: splicing DELETES the file's tail. The real overlap
  // observed is a few hundred characters (the model re-emits the current block);
  // beyond that, a chance match would destroy good code. When in
  // doubt we glue end to end: a bit of duplication is visible and fixable,
  // vanished code is not.
  if (meilleur && base.length - meilleur.base <= RECOL_MAX_SUPPRIME) {
    return base.slice(0, meilleur.base) + suite.slice(meilleur.suite);
  }
  return raboutLigne(base, suite);
}

/** Splices a continuation that REPEATS the end of the cut line.
 *
 * The token ceiling lands mid-line, and the model often resumes
 * the word (or line) it had stopped at: measured on 2026-10-01 on MiMo,
 * « p_del » + « del = sub.add_parser(…) » gave « p_deldel », NameError at
 * launch. Line splicing sees nothing (fewer than 3 common lines).
 * We look for the longest start of the continuation that already ends the cut
 * line; at least 3 characters, so as not to trim a legitimate character by chance. */
function raboutLigne(base: string, suite: string): string {
  const fin = base.slice(base.lastIndexOf("\n") + 1);
  if (!fin.trim()) return base + suite;
  const tete = suite.split("\n", 1)[0];
  for (let k = Math.min(fin.length, tete.length); k >= 3; k--) {
    if (fin.endsWith(tete.slice(0, k))) return base + suite.slice(k);
  }
  return base + suite;
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
  const debut = src.search(/[{[]/);
  if (debut < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = debut; i < src.length; i++) {
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
      if (depth === 0) return src.slice(debut, i + 1);
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
function finObjet(src: string, debut: number): number | null {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = debut; i < src.length; i++) {
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
function candidatsQuestions(src: string): { texte: string; debut: number; fin: number }[] {
  const out: { texte: string; debut: number; fin: number }[] = [];
  const rx = /\{\s*"(?:question|questions)"\s*:/gi;
  let m: RegExpExecArray | null;
  while ((m = rx.exec(src))) {
    const debut = m.index;
    const fin = finObjet(src, debut);
    out.push({ texte: src.slice(debut, fin ?? src.length), debut, fin: fin ?? src.length });
    rx.lastIndex = debut + 1;
  }
  return out;
}

/** The questions carried by a JSON text, through the repair chain.
 *
 * Three defects seen in production, in order: raw line breaks inside a
 * string, junk after the object, object never closed. What remains
 * unrecoverable yields ZERO questions — other candidates will carry the
 * others. */
function questionsDe(corps: string): AskQ[] {
  let obj: unknown;
  try {
    obj = JSON.parse(corps);
  } catch {
    const repare = escapeRawControlChars(corps);
    try {
      obj = JSON.parse(repare);
    } catch {
      try {
        obj = JSON.parse(firstJsonValue(repare) ?? balanceJson(repare));
      } catch {
        try {
          obj = JSON.parse(reparerFermetures(repare));
        } catch {
          return [];
        }
      }
    }
  }
  const brut = obj as { question?: unknown; questions?: unknown } | null;
  const liste: unknown[] = Array.isArray(brut?.questions) ? brut.questions : brut?.question ? [brut] : [];
  return liste
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
 *  (`estBlocQuestions`): a block that can carry a questionnaire does not
 *  protect its contents, all the others do. */
function bornesFichiers(content: string): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (const m of content.matchAll(/```([\w+-]*)[ \t]*\n([\s\S]*?)(?:```|$)/g)) {
    if (estBlocQuestions(m[1] || "", m[2])) continue;
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
export function contientQuestions(content: string): boolean {
  const fichiers = bornesFichiers(content);
  return candidatsQuestions(content).some(
    (c) => !fichiers.some(([d, f]) => c.debut >= d && c.fin <= f),
  );
}

/** Does this answer leave an UNFINISHED file?
 *
 * A questionnaire is not a file: never « resume » it. An HTML file that
 * does end with `</html>` is not unfinished either (the model merely
 * forgot to close its block). */
export function reponseIncomplete(content: string): boolean {
  if (contientQuestions(content)) return false;
  const ouvert = openCodeFence(content);
  if (ouvert) return !/<\/html\s*>\s*$/i.test(ouvert.body.trimEnd());
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
  const fichiers = bornesFichiers(content);
  const questions: AskQ[] = [];
  const vus = new Set<string>();
  let premier = content.length;   // début du premier fragment de question

  const ramasse = (corps: string, offset: number) => {
    for (const c of candidatsQuestions(corps)) {
      const debut = offset + c.debut;
      const fin = offset + c.fin;
      if (fichiers.some(([d, f]) => debut >= d && fin <= f)) continue;   // c'est un fichier
      premier = Math.min(premier, debut);
      for (const q of questionsDe(c.texte)) {
        // Deduplication key, never displayed: deliberately locale-free.
        const cle = q.question.toLowerCase();
        if (vus.has(cle) || questions.length >= MAX_ASK_QUESTIONS) continue;
        vus.add(cle);
        questions.push(q);
      }
    }
  };

  // 1) The blocks we ASK it to write (we keep their place for the prose).
  for (const m of content.matchAll(/```ask[ \t]*\n([\s\S]*?)(?:```|$)/gi)) {
    premier = Math.min(premier, m.index ?? 0);
    ramasse(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  for (const m of content.matchAll(/```(?:json)?[ \t]*\n(\s*\{\s*"questions"\s*:\s*\[[\s\S]*?)(?:```|$)/g)) {
    premier = Math.min(premier, m.index ?? 0);
    ramasse(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  // Degenerate form measured on MiMo on 2026-10-02: « ask » in inline code (or
  // bare) followed by the JSON WITHOUT a closing fence. The `{"questions": [
  // guard avoids swallowing a real file along the way.
  for (const m of content.matchAll(/(?:^|\n)[ \t]*`?ask`?[ \t]*\n(\s*\{\s*"questions"\s*:\s*\[[\s\S]*?)(?:```|$)/gi)) {
    premier = Math.min(premier, m.index ?? 0);
    ramasse(m[1], (m.index ?? 0) + m[0].length - m[1].length);
  }
  // 2) And above all: the question objects SET IN THE TEXT, wherever they
  //    are — this is what remains when the opening block failed.
  ramasse(content, 0);

  if (!questions.length) return null;
  // The questionnaire prose = what PRECEDES the first question fragment. The
  // instruction allows only ONE intro sentence before the block; we cut the
  // JSON debris there (bracket/brace followed by a quote, « ``` » marker): a
  // model in difficulty mixes its intro with its questions.
  let prose = content.slice(0, premier);
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
  if (estBlocQuestions(first, rest.slice(nl + 1))) return null;   // handled by parseAsk
  return { lang: first || "text", body: rest.slice(nl + 1), start };
}

