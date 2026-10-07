// Pure helpers shared by the playground page and its extracted child
// components (message bubbles, document panel): artifact parsing, file-state
// reconstruction, truncation heuristics. Extracted VERBATIM from
// app/(app)/playground/page.tsx — no behaviour change, see that file for the
// measurements behind each heuristic.

import type { Attachment, ChatMsg, Settings } from "@/lib/types";
import type { EtapeWeb } from "@/lib/api";
import {
  contenuCloture,
  corpsDeSuite,
  estBlocQuestions,
  parseEdits,
  recoller,
  reponseIncomplete,
} from "@/lib/playground-parsers";

// Something the assistant produced worth showing in the side panel (canvas/
// artifact style) and copying in one click: a code "file" or a long "document"
// (e.g. a rewritten/reformatted text).
export type Artifact =
  | { kind: "code"; title: string; lang: string; content: string }
  | { kind: "doc"; title: string; content: string };

// Below this length a document-task answer is probably a clarifying question →
// keep it inline rather than filing it as a document.
export const DOC_MIN_CHARS = 400;
/** Does the answer stop in the middle of a file?
 *
 * The token ceiling is not the only way to end up truncated: a model of
 * this size sometimes lets go in the middle of a large file and emits its end
 * of sequence mid-expression. The counter then says « terminé » while the
 * file is unusable, and nothing allowed a resume.
 */
/** Is the script of this HTML page closed?
 *
 * Seen in production: the model writes `</script></body></html>` while a
 * brace remains open in the middle. The file looks finished — it does end with
 * `</html>` — but its JavaScript does not run at all (« Unexpected end of
 * input »), blank canvas, dead page. Checking the end tag is therefore not enough.
 *
 * We CANNOT rely on `new Function` to know this: the portal's CSP
 * (`script-src 'self' 'nonce-…'`, without `unsafe-eval`) forbids it in the browser,
 * and the thrown exception is then not even a SyntaxError. So we count the
 * blocks ourselves, skipping strings, templates, comments and regular
 * expression literals — otherwise the slightest brace in a text would skew
 * everything.
 */
export function profondeurFinale(code: string): number {
  let i = 0, prof = 0;
  const n = code.length;
  // Stack of template literals `...${ ... }...`: inside a ${}, we are back in code.
  const gabarits: number[] = [];
  let precedent = "";                       // last significant character
  const avantRegex = /[(,=:[!&|?{};+\-*%~^<>]/;
  const motsAvantRegex = /(?:^|[^\w$])(?:return|typeof|instanceof|in|of|new|delete|void|do|else|case|yield|await)$/;
  while (i < n) {
    const c = code[i];
    // — comments
    if (c === "/" && code[i + 1] === "/") { while (i < n && code[i] !== "\n") i++; continue; }
    if (c === "/" && code[i + 1] === "*") { i += 2; while (i < n && !(code[i] === "*" && code[i + 1] === "/")) i++; i += 2; continue; }
    // — strings
    if (c === "'" || c === '"') {
      const q = c; i++;
      while (i < n && code[i] !== q) { if (code[i] === "\\") i++; i++; }
      i++; precedent = "x"; continue;
    }
    // — templates
    if (c === "`") {
      i++;
      for (;;) {
        if (i >= n) return 1;               // template never closed → truncated
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === "`") { i++; break; }
        if (code[i] === "$" && code[i + 1] === "{") { gabarits.push(prof); prof++; i += 2; break; }
        i++;
      }
      precedent = "x"; continue;
    }
    // — regular expression literal
    if (c === "/" && (precedent === "" || avantRegex.test(precedent)
                      || motsAvantRegex.test(code.slice(Math.max(0, i - 12), i)))) {
      i++;
      let classe = false;
      while (i < n) {
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === "[") classe = true;
        else if (code[i] === "]") classe = false;
        else if (code[i] === "/" && !classe) { i++; break; }
        else if (code[i] === "\n") break;   // not a regex after all
        i++;
      }
      precedent = "x"; continue;
    }
    if (c === "{" || c === "(" || c === "[") prof++;
    else if (c === "}" || c === ")" || c === "]") {
      prof--;
      // A brace closing a `${…}` drops back into the template.
      if (gabarits.length && prof === gabarits[gabarits.length - 1]) {
        gabarits.pop();
        i++;
        // back into the template until its backtick
        for (;;) {
          if (i >= n) return 1;
          if (code[i] === "\\") { i += 2; continue; }
          if (code[i] === "`") { i++; break; }
          if (code[i] === "$" && code[i + 1] === "{") { gabarits.push(prof); prof++; i += 2; break; }
          i++;
        }
        precedent = "x"; continue;
      }
    }
    if (!/\s/.test(c)) precedent = c;
    i++;
  }
  return prof;
}

export const memoScript = new Map<string, boolean>();

export function scriptCasse(contenu: string): boolean {
  const cache = memoScript.get(contenu);
  if (cache !== undefined) return cache;
  let casse = false;
  for (const m of contenu.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const attrs = m[1] || "";
    if (/\bsrc\s*=/i.test(attrs)) continue;                       // external script
    if (/type\s*=\s*["']?(?!text\/javascript|application\/javascript)[^"'\s>]+/i.test(attrs)) continue;
    const code = m[2];
    if (!code.trim()) continue;
    // A block still open at the end = unusable file. We only report THIS
    // direction: an excess of closers would more likely come from an imperfect
    // reading on our side than from a real defect.
    if (profondeurFinale(code) > 0) { casse = true; break; }
  }
  if (memoScript.size > 40) memoScript.clear();
  memoScript.set(contenu, casse);
  return casse;
}
/** What a web-search step displays, in plain words.
 *
 * The model spends several tens of seconds searching and reading before
 * answering. Without this feed, the wait is totally silent and nobody knows
 * what is going on — this was the first usage feedback we got on it.
 */
export function libelleEtapeWeb(
  e: EtapeWeb,
  t: (s: string) => string,
): { texte: string; fini: boolean } {
  const outil = e.outil;
  switch (e.etape) {
    case "recherche":
      return { fini: false,
        texte: `${outil} · ` + t("recherche « {q} »").replace("{q}", e.question ?? "") };
    case "recherche_finie":
      return { fini: true, texte: `${outil} · ` + (e.erreur
        ? t("recherche impossible : {e}").replace("{e}", e.erreur)
        : t("{n} résultat(s) pour « {q} »")
            .replace("{n}", String(e.nombre ?? 0)).replace("{q}", e.question ?? "")) };
    case "lecture":
      return { fini: false,
        texte: `${outil} · ` + t("lecture de {n} page(s)")
          .replace("{n}", String((e.urls ?? []).length)) };
    case "lecture_finie": {
      const rates = (e.echecs ?? []).length;
      return { fini: true, texte: `${outil} · ` + (e.erreur
        ? t("lecture impossible : {e}").replace("{e}", e.erreur)
        : t("{n} page(s) lue(s)").replace("{n}", String(e.lues ?? 0))
          + (rates ? t(", {n} inaccessible(s)").replace("{n}", String(rates)) : "")) };
    }
    case "generation":
      return { fini: false,
        texte: `${outil} · ` + t("génération de l'image « {q} »")
          .replace("{q}", e.question ?? "") };
    case "generation_finie":
      return { fini: true, texte: `${outil} · ` + (e.erreur
        ? t("génération impossible : {e}").replace("{e}", e.erreur)
        : t("{n} image(s) générée(s)").replace("{n}", String((e.images ?? []).length))) };
    case "generation_video":
      return { fini: false,
        texte: `${outil} · ` + t("génération de la vidéo « {q} »")
          .replace("{q}", e.question ?? "") };
    case "generation_video_finie":
      return { fini: true, texte: `${outil} · ` + (e.erreur
        ? t("génération vidéo impossible : {e}").replace("{e}", e.erreur)
        : t("{n} vidéo(s) générée(s)").replace("{n}", String((e.videos ?? []).length))) };
    case "ocr":
      return { fini: false,
        texte: `${outil} · ` + t("lecture du document")
          + (e.question ? ` « ${e.question} »` : "") };
    case "ocr_finie":
      return { fini: true, texte: `${outil} · ` + (e.erreur
        ? t("lecture impossible : {e}").replace("{e}", e.erreur)
        : t("{n} caractère(s) extraits").replace("{n}", String(e.caracteres ?? 0))) };
    default:
      return { texte: outil, fini: true };
  }
}
/** Did the model announce something and then close its turn?
 *
 * Seen in production: « Bien sûr ! Quelques précisions pour bien t'aider : » — 13
 * tokens, then a normal model stop (the server log confirms a clean stop,
 * no network cut, no ceiling). The announced questions block never arrives and
 * the user is left with an introductory sentence all by itself.
 *
 * A WHOLE answer that ends with a colon, without the slightest code block,
 * is never a finished answer: it promises a continuation that never came.
 */
export function tourAvorte(m: ChatMsg | undefined): boolean {
  if (!m || m.role !== "assistant") return false;
  const t = m.content.trim();
  // Beyond that, it is a real answer that happens to end with « : » (an introduced
  // list, for example) — not an aborted turn.
  if (!t || t.length > 400 || t.includes("```")) return false;
  if (/[:：]$/.test(t)) return true;
  // Same thing, in another form: the NAME of the announced file, then nothing.
  // Measured on 2026-10-02 on MiMo: « `roles/nginx_reverse_proxy/tasks/main.yml` »
  // — 8 tokens, normal model stop, and the expected Ansible role never arrives.
  // Required: an extension starting with a letter, AND backticks or a
  // directory — otherwise « 3.14 » or « google.com » would be redone in a loop.
  const derniere = t.split("\n").pop()!.trim();
  const nu = derniere.replace(/^`(.*)`$/, "$1");
  const chemin = /^[\w.\/-]*[\w-]\.[A-Za-z][A-Za-z0-9]{0,7}$/.test(nu) || BARE_FILES.test(nu);
  return chemin && (nu !== derniere || nu.includes("/"));
}

export const PROMPT_REPRISE = "Continue exactement là où tu t'es arrêté";
export const PROMPT_REPRISE_COMPLET =
  PROMPT_REPRISE + ", sans rien répéter et sans réintroduire ta réponse. "
  + "Reprends au caractère suivant, et va jusqu'au bout du fichier.";
export const PROMPT_INTEGRAL =
  "Tu viens d'abreger ce fichier alors que je l'ai demande complet. "
  + "Reecris-le en ENTIER, de la premiere a la derniere ligne, sans aucune coupure, "
  + "sans resume et sans « reste inchange ». Il n'y a aucune limite d'affichage.";
export const REPRISE_INSTRUCTION = `Your previous reply was cut off in the middle of a file. Output ONLY the missing remainder of that file, starting at the exact character where you stopped. Do not repeat anything already written, do not re-introduce, do not summarise, and do not use an edit block — just continue the raw content until the file is complete.`;

/** Consecutive automatic resumes before handing back to the user. */
// Raised from 3 to 10: a single large file takes about ten segments
// (measured: 2 400 to 14 000 tokens per resume), and at 3 the chain still
// handed back on an unfinished file. Still bounded: a resume
// that loops without making progress must return to the user, not run forever.
export const MAX_REPRISES_AUTO = 10;

/** Is this hidden message the resume request issued by « Continuer » ? */
export function estReprise(m: ChatMsg | undefined): boolean {
  return !!m && m.role === "user" && !!m.hidden && m.content.startsWith(PROMPT_REPRISE);
}

/** Cuts what the model wrote AFTER its questions block.
 *
 * The instruction asks it to stop at the block, but it sometimes starts again
 * and generates up to the token ceiling (seen: 4096 for a ~150 question).
 * This text is not displayed — yet it ends up in the history
 * sent back to the model on the next turn, which then answers anything or
 * nothing. We keep the introduction and the block, we drop the rest.
 */
export function trimAfterAsk(content: string): string {
  const i = content.indexOf("```ask");
  if (i < 0) return content;
  const close = content.indexOf("```", i + 6);
  return close < 0 ? content : content.slice(0, close + 3);
}

// Whether the user's request is a "document" task (correct / rewrite / reformat /
// draft / "make a document/report/note…"). Only then is the answer treated as a
// document. Accent- and language-insensitive (FR + EN). Plain "explain"/"summarize"
// stays inline.
export function isDocTask(prompt: string): boolean {
  const p = prompt.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  // (a) Correction / rewrite / reformat tasks.
  if (/(corrig|reformul|reecri|reecrire|redig|remet(s)? en forme|met(s)? en forme|mise en forme|orthograph|\brelis\b|relire|proofread|rewrite|re-?format|rephrase|\bcorrect\b|clean ?up|\bedit this\b|\bfix the\b)/.test(p)) return true;
  // (b) "Produce a document / report / note / letter / …": a create verb near a doc noun.
  if (/(fais|fait|faire|cree|creer|genere|generer|ecri|redig|prepar|make|create|write|draft|produce|compose|generate|prepare)[\s\S]{0,30}(document|rapport|report|\bnote\b|compte[- ]?rendu|fiche|guide|essai|essay|lettre|letter|courriel|e-?mail|\bmail\b|article|synthese|memo|dossier|cahier|resume)/.test(p)) return true;
  return false;
}

// Turn an assistant answer into prose (chat) + artifacts (side panel).
// Deterministic — no reliance on the model's own formatting:
//  - Document task with a substantial answer → the WHOLE answer is ONE document
//    artifact; the chat shows only a short line + a card (no duplication, no
//    stray code-block cards for tables/ascii inside the document).
//  - Otherwise → substantial fenced code blocks become file artifacts, the rest
//    of the prose stays inline.
// Name a generated document from its own content: the first Markdown heading,
// else the first non-empty line, stripped of Markdown decoration. Returns
// "Document" while a stream hasn't produced a usable title line yet.
export function docTitleFromContent(content: string): string {
  const text = content.trim();
  if (!text) return "Document";
  const heading = text.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/m);
  let raw = heading ? heading[1] : (text.split("\n").find((l) => l.trim().length > 0) ?? "");
  raw = raw
    .replace(/[*_`~#>]/g, "")          // md emphasis / fences / quotes / hashes
    .replace(/^\s*[-•]+\s+/, "")       // bullet markers
    .replace(/^\s*\d+[.)]\s+/, "")     // numbered markers
    .replace(/\s+/g, " ")
    .trim();
  if (!raw) return "Document";
  return raw.length > 60 ? raw.slice(0, 57).trimEnd() + "…" : raw;
}

// A filesystem-safe slug for the download filename.
export function slugify(s: string): string {
  const base = s
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "") // strip accents
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || "document";
}
// Extensions recognized as FILES. Deliberately a closed list: without
// it, « ansible.builtin.reboot » or « os_family['debian'] » would pass for
// file names and produce absurd titles.
export const FILE_EXT = new RegExp(
  "\\.(ya?ml|json|jsonc|toml|ini|cfg|conf|env|py|js|mjs|cjs|ts|tsx|jsx|sh|bash|zsh|" +
  "go|rs|rb|php|java|kt|c|h|cpp|sql|html|css|scss|md|txt|log|xml|service|tf|gradle)$",
  "i");
export const BARE_FILES = /^(Dockerfile|Makefile|Vagrantfile|Jenkinsfile|Procfile)$/i;

/** File name announced right BEFORE a code block.
 *
 * A model almost always writes « ### 2. `tasks/main.yml` » then the block.
 * Without reading this context, artifacts were named « file 1 », « yaml · 2 »… —
 * cards one could no longer tell which file they belonged to,
 * while the prose just above did name the files.
 */
export function titleFromContext(before: string): string {
  // Only look back to the PREVIOUS block: beyond that, we pick up
  // comments written INSIDE the previous block (« # defaults/main.yml »)
  // and name the current file with the previous one's name.
  const finBlocPrecedent = before.lastIndexOf("```");
  const zone = finBlocPrecedent >= 0 ? before.slice(finBlocPrecedent + 3) : before;
  const tail = zone.slice(-300);
  const candidats: string[] = [];
  // 1) between backticks — the most reliable form
  for (const m of tail.matchAll(/`([^`\n]{1,80})`/g)) candidats.push(m[1].trim());
  // 2) otherwise a token that looks like a path
  for (const m of tail.matchAll(/(?:^|[\s(*_"'>])([\w.-]+(?:\/[\w.-]+)*)(?=[\s:,)*_"'.]|$)/gm)) {
    candidats.push(m[1].trim());
  }
  for (let i = candidats.length - 1; i >= 0; i--) {
    const c = candidats[i].replace(/^[.\/]+/, "");
    if (!c || c.length > 80) continue;
    if (FILE_EXT.test(c) || BARE_FILES.test(c)) return c;
  }
  return "";
}

/** `t` is passed as a parameter (module function): the fallback title of
 *  unnamed artifacts follows the displayed language, including on download. */
export function parseArtifacts(content: string, allowDoc: boolean, t: (s: string) => string): { prose: string; artifacts: Artifact[] } {
  const text = content.trim();
  // A message containing a real code block is a FILE, never a
  // document: taking it for a document returned the whole message — question
  // included — under a .md name. It depended on `allowDoc`, hence on the
  // PREVIOUS message, hence behavior that changed after an F5.
  const aDuCode = /```[^\n`]*\n[\s\S]{120,}?```/.test(text) || /<!DOCTYPE html|<html[\s>]/i.test(text);
  if (allowDoc && !aDuCode && text.length >= DOC_MIN_CHARS) {
    return { prose: "", artifacts: [{ kind: "doc", title: docTitleFromContent(text), content: text }] };
  }
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g;
  const artifacts: Artifact[] = [];
  let prose = "";
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = fence.exec(content)) !== null) {
    const body = m[2].replace(/\n$/, "");
    const info = m[1].trim();
    const first = info.split(/\s+/)[0] || "";
    // ```ask and ```edit are PROTOCOL, never files. `edit` was not a
    // problem as long as an unclosed block was not extracted; since we force-close
    // fences, a truncated edit block came out as « fichier-2.txt ».
    if (first === "edit" || estBlocQuestions(first, body)) continue;
    let lang = first;
    let title = "";
    if (first.includes(".")) { title = first; lang = first.split(".").pop() || ""; }
    const named = info.match(/(?:title|file|filename)=(\S+)/i);
    if (named) title = named[1];
    // The name announced in the prose just above beats a number.
    if (!title) title = titleFromContext(content.slice(0, m.index));
    // A block WHOSE FILE NAME IS ANNOUNCED is a file, even a short one: otherwise
    // an Ansible role came out with two files as cards and the third — shorter —
    // left in the chat, for the same thing. A short anonymous snippet
    // however stays in the thread: it is an illustration, not a deliverable.
    const substantial = !!title || body.length >= 200 || body.split("\n").length >= 6;
    if (!substantial) continue;
    n += 1;
    // Fallback name: a real file name, not a label. « html · 1 » used to
    // download as « html · 1.txt » — neither readable nor openable.
    if (!title) {
      const info = LANG_INFO[(lang || "").toLowerCase()];
      // A lone HTML page is named index.html — that is what one expects of it,
      // and it opens as is. Others keep a neutral numbered name.
      // `n` was just incremented: the FIRST block has n === 1. With `n === 0`
      // the condition was never true and an HTML page with no announced name
      // always came out as « fichier-2.html ».
      if (info?.ext === "html" && n === 1) title = "index.html";
      // Translated fallback name: a downloaded file must not keep a French
      // name in English mode.
      else title = info
        ? t("fichier-{n}.{ext}").replace("{n}", String(n + 1)).replace("{ext}", info.ext)
        : t("fichier-{n}.txt").replace("{n}", String(n + 1));
    }
    artifacts.push({ kind: "code", title, lang: lang || "text", content: body });
    prose += content.slice(lastIndex, m.index);
    lastIndex = fence.lastIndex;
  }
  prose += content.slice(lastIndex);

  // Safety net: an HTML document written WITHOUT a code block. The model often
  // forgets the closing for a large file — then no file came out
  // at all, so no card, no preview, no download. The user
  // fell back on exporting the conversation (button since removed, it lent
  // itself to confusion): hence a .md containing the question and the prose.
  if (!artifacts.length) {
    const html = content.match(/<!DOCTYPE html[\s\S]*?<\/html\s*>|<html[\s\S]*?<\/html\s*>/i);
    if (html && html[0].length >= 200) {
      const avant = content.slice(0, html.index ?? 0);
      const titre = titleFromContext(avant) || "page.html";
      artifacts.push({
        kind: "code",
        title: /\.html?$/i.test(titre) ? titre : "page.html",
        lang: "html",
        content: html[0],
      });
      return { prose: (avant + content.slice((html.index ?? 0) + html[0].length)).trim(), artifacts };
    }
  }
  return { prose: prose.trim(), artifacts };
}

// Extension and content type per language. Without an extension, the browser adds
// « .txt » from the MIME type: a file named « html · 1 » downloaded as
// « html · 1.txt », unreadable and unopenable.
export const LANG_INFO: Record<string, { ext: string; mime: string }> = {
  html: { ext: "html", mime: "text/html" },
  htm: { ext: "html", mime: "text/html" },
  css: { ext: "css", mime: "text/css" },
  javascript: { ext: "js", mime: "text/javascript" },
  js: { ext: "js", mime: "text/javascript" },
  typescript: { ext: "ts", mime: "text/plain" },
  ts: { ext: "ts", mime: "text/plain" },
  tsx: { ext: "tsx", mime: "text/plain" },
  jsx: { ext: "jsx", mime: "text/plain" },
  json: { ext: "json", mime: "application/json" },
  yaml: { ext: "yml", mime: "text/yaml" },
  yml: { ext: "yml", mime: "text/yaml" },
  toml: { ext: "toml", mime: "text/plain" },
  python: { ext: "py", mime: "text/x-python" },
  py: { ext: "py", mime: "text/x-python" },
  bash: { ext: "sh", mime: "text/x-shellscript" },
  sh: { ext: "sh", mime: "text/x-shellscript" },
  shell: { ext: "sh", mime: "text/x-shellscript" },
  sql: { ext: "sql", mime: "text/plain" },
  xml: { ext: "xml", mime: "text/xml" },
  markdown: { ext: "md", mime: "text/markdown" },
  md: { ext: "md", mime: "text/markdown" },
  go: { ext: "go", mime: "text/plain" },
  rust: { ext: "rs", mime: "text/plain" },
  rs: { ext: "rs", mime: "text/plain" },
  java: { ext: "java", mime: "text/plain" },
  c: { ext: "c", mime: "text/plain" },
  cpp: { ext: "cpp", mime: "text/plain" },
  php: { ext: "php", mime: "text/plain" },
  ruby: { ext: "rb", mime: "text/plain" },
};

/** Actually downloadable name: no space nor « · », and always an extension
 *  consistent with the language. */
export function nomTelechargeable(titre: string, lang: string): string {
  if (/\.[a-z0-9]{1,6}$/i.test(titre)) return titre;
  const info = LANG_INFO[(lang || "").toLowerCase()];
  const base = titre.replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "fichier";
  return info ? `${base}.${info.ext}` : `${base}.txt`;
}

export function mimePourLangage(lang: string): string {
  return LANG_INFO[(lang || "").toLowerCase()]?.mime ?? "text/plain";
}

/** Is this block a snippet of an already known file, rather than a version? */
export function estFragment(
  ancien: { content: string; lang: string } | undefined,
  nouveau: string,
): boolean {
  if (!ancien) return false;
  // Deliberately conservative thresholds: we only reject an update if the
  // known file is already substantial AND the new block is less than
  // two thirds of it. A real rewrite that shortens a bit still passes.
  return ancien.content.length > 800 && nouveau.length < ancien.content.length * 0.66;
}

// Title given for lack of a better one (« fichier-2.html »): the model did not
// name its block. Both forms — French and English — exist, the name is
// built in the displayed language (parseArtifacts receives t).
export const TITRE_GENERIQUE = /^(?:fichier|file)-\d+\.[a-z0-9]{1,6}$/i;

/** The files for which THIS message contains only a snippet.
 *
 * Two forms, both seen in the wild:
 *  - the block carries the file name but is much shorter → named snippet;
 *  - the block is not named at all (« voici la partie corrigée ») while a
 *    much larger file of the same language already exists → anonymous snippet.
 */
export function fragmentsDuMessage(messages: ChatMsg[], index: number, t: (s: string) => string): string[] {
  const m = messages[index];
  if (!m || m.role !== "assistant") return [];
  const avant = fichiersJusqua(messages, index - 1, t);
  const noms: string[] = [];
  for (const a of parseArtifacts(contenuCloture(m.content), false, t).artifacts) {
    if (a.kind !== "code") continue;
    if (estFragment(avant.get(a.title), a.content)) { noms.push(a.title); continue; }
    if (!TITRE_GENERIQUE.test(a.title)) continue;
    // Anonymous block: which known file of the same language could it belong to?
    const candidat = [...avant.entries()]
      .filter(([, f]) => f.lang === a.lang)
      .find(([, f]) => estFragment(f, a.content));
    if (candidat) noms.push(candidat[0]);
  }
  if (noms.length) return noms;

  // Last case, the most frequent: a SHORT unnamed block (« voici la partie
  // corrigée »). Too small to become a file, it stays in the thread — but
  // the user, meanwhile, expected their fixed file. So we look at raw blocks,
  // not only those promoted to files.
  if (!avant.size) return [];
  for (const f of m.content.matchAll(/```([^\n`]*)\n([\s\S]*?)```/g)) {
    const lang = (f[1].trim().split(/\s+/)[0] || "").toLowerCase();
    if (lang === "edit" || estBlocQuestions(lang, f[2])) continue;
    const corps = f[2];
    const candidat = [...avant.entries()].find(
      ([, fic]) => (!lang || fic.lang.toLowerCase() === lang) && estFragment(fic, corps));
    if (candidat) return [candidat[0]];
  }
  return [];
}

/** Current state of each file in the conversation, up to the `index` message.
 *
 * DERIVED from the thread, never stored: a file = its last complete version, to
 * which the subsequent edits are applied in order. Nothing to
 * synchronize, hence nothing that can go out of sync — reloading the
 * conversation rebuilds exactly the same state.
 */
export function fichiersJusqua(
  messages: ChatMsg[],
  index: number,
  t: (s: string) => string,
): Map<string, { content: string; lang: string }> {
  const fichiers = new Map<string, { content: string; lang: string }>();
  // The file left in the lurch by the previous message: a resume completes it
  // instead of opening a second file with half of the content.
  let inacheve: string | null = null;
  for (let i = 0; i <= index && i < messages.length; i++) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    // A resume only glues onto a file REALLY left open.
    // Falling back to the last known file seemed prudent, but glued
    // the continuation onto an already finished file: two </html>, unbalanced braces.
    const cible = inacheve ? fichiers.get(inacheve) : undefined;
    // A protocol block is NOT the file's continuation: the model answered
    // « Continue » with a ```edit (seen in production). Gluing it would have
    // injected JSON in the middle of the HTML — we treat it as a normal message.
    const protocole = /```(?:edit|ask)\b/.test(m.content);
    if (estReprise(messages[i - 1]) && !protocole && !cible) {
      // Resume with nothing to complete (the file was already finished): its content
      // is not a file. Making one produced the « fichier-2.txt » card
      // filled with half a script.
      inacheve = null;
      continue;
    }
    if (cible && !protocole && estReprise(messages[i - 1])) {
      const fusion: string = recoller(cible.content, corpsDeSuite(m.content));
      fichiers.set(inacheve!, { ...cible, content: fusion });
      // A resume can itself be cut. Its fence count says nothing
      // (it starts in the middle of a block): it is the rebuilt file that
      // decides whether it stays open — OR the token ceiling itself. The rebuilt
      // file has no fences left, and `reponseIncomplete` then only
      // recognizes an HTML without </html>: a Python script cut by the ceiling passed
      // for finished, and the NEXT resume was dropped — the end of the file
      // disappeared (measured on 2026-10-01 on MiMo: file stopped at `p_del`).
      inacheve = m.truncated || reponseIncomplete(fusion) ? inacheve : null;
      continue;
    }
    // A SINGLE parse per message (the old `fichierInacheve` re-parsed the same
    // content just before): `fichiersJusqua` is called for every message on
    // every render, so the duplicate cost quadratic per token received, and
    // `fusionDuMessage`/`appliquerEdits` also call it twice per message.
    const fences = m.content.match(/```/g);
    const arts = parseArtifacts(contenuCloture(m.content), false, t).artifacts;
    const dernierArt = arts[arts.length - 1];
    // The file this message leaves unfinished, if any: odd fence count
    // (unclosed block) and last code-type artifact.
    inacheve = (fences && fences.length % 2 === 1 && dernierArt && dernierArt.kind === "code")
      ? dernierArt.title : null;
    for (const [rang, a] of arts.entries()) {
      if (a.kind !== "code") continue;
      // The block the cut left open is the version BEING written,
      // not a snippet: without this, the resume glued onto the previous complete
      // version. We still keep a floor, so that a stub of
      // a few lines does not destroy a finished file.
      const precedent = fichiers.get(a.title);
      if (rang === arts.length - 1 && a.title === inacheve
          && (!precedent || a.content.length >= precedent.content.length * 0.25)) {
        fichiers.set(a.title, { content: a.content, lang: a.lang });
        continue;
      }
      // A block MUCH shorter than an already known file of the same name is a
      // SNIPPET (« voici la partie corrigée »), not a new version. Taking it
      // for the file replaced 400 lines with 20, and the panel
      // displayed this stub as if it were the file.
      if (estFragment(fichiers.get(a.title), a.content)) continue;
      fichiers.set(a.title, { content: a.content, lang: a.lang });
    }
    for (const e of parseEdits(m.content)) {
      // The model may name « index.html » a file saved as
      // « site/index.html »: we fall back to a suffix match.
      const cle =
        (fichiers.has(e.file) && e.file) ||
        [...fichiers.keys()].find((k) => k === e.file || k.endsWith("/" + e.file) || e.file.endsWith("/" + k));
      if (!cle) continue;
      const cible = fichiers.get(cle)!;
      if (!cible.content.includes(e.find)) continue;   // anchor not found: we invent nothing
      fichiers.set(cle, { ...cible, content: cible.content.replace(e.find, e.replace) });
    }
  }
  return fichiers;
}

/** The file a resume message just completed.
 *
 * This message only contains the end of the file: what must be shown is the
 * whole rebuilt file, not the half it carries.
 */
export function fusionDuMessage(messages: ChatMsg[], index: number, t: (s: string) => string): Artifact[] {
  const m = messages[index];
  if (!m || m.role !== "assistant" || !estReprise(messages[index - 1])) return [];
  const avant = fichiersJusqua(messages, index - 1, t);
  const apres = fichiersJusqua(messages, index, t);
  const out: Artifact[] = [];
  for (const [titre, f] of apres) {
    const a = avant.get(titre);
    if (a && a.content !== f.content) {
      out.push({ kind: "code", title: titre, lang: f.lang, content: f.content });
    }
  }
  return out;
}

/** Does this message leave a file unfinished, resumes included?
 *  (`t` only to pass through to fusionDuMessage: only the content matters here.) */
export function fichierLaisseOuvert(messages: ChatMsg[], index: number, t: (s: string) => string): boolean {
  const m = messages[index];
  if (!m || m.role !== "assistant") return false;
  // A resume starts IN THE MIDDLE of a block: it has no opening fence, so
  // its fence count is always odd. Trusting it restarted one resume after
  // another while the file was closed. Only the rebuilt file decides.
  if (estReprise(messages[index - 1])) {
    const fusion = fusionDuMessage(messages, index, t);
    return fusion.length ? fusion.some((f) => reponseIncomplete(f.content)) : false;
  }
  return reponseIncomplete(m.content);
}

/* The model that ADMITS it abbreviated.
 *
 * Case seen in prod on 22/08: « Le fichier est trop long pour être affiché en
 * entier ici », followed by a truncated file. Such a message can perfectly well
 * close its fence — the file then looks finished and fichierLaisseOuvert() sees
 * nothing, while the model itself just said content is missing.
 *
 * Each pattern is an explicit ADMISSION, never an ordinary phrasing: « version
 * simplifiée » or « pour résumer » are deliberately absent, they appear
 * in perfectly complete answers and would trigger resumes in a
 * loop (already experienced with proseIncomplete, which destroyed an entire
 * conversation in four resumes before being removed).
 */
export const ABANDON_DECLARE =
  /trop\s+(?:long|volumineux|gros)[^.\n]{0,80}?(?:affich|ici\b|ce\s+message)/i;
export const ABANDON_DECLARE_ALT = [
  /too\s+(?:long|large)[^.\n]{0,80}?(?:display|show\b|here\b|message)/i,
  /je\s+ne\s+peux\s+pas\s+(?:l['’]?)?(?:affich|[ée]crire)[^.\n]{0,60}?(?:int[ée]gralit|en\s+entier)/i,
  /(?:reste|suite)\s+du\s+(?:code|fichier)\s+(?:inchang|identique|omis)/i,
  /rest\s+of\s+the\s+(?:code|file)[^.\n]{0,30}?unchanged/i,
  /\.\.\.\s*\(\s*(?:suite|reste)/i,
];

export function abandonDeclare(content: string): boolean {
  // Only on a message that claims to deliver code: the same sentence in
  // a prose answer signals nothing to resume.
  if (!content.includes("```")) return false;
  return ABANDON_DECLARE.test(content) || ABANDON_DECLARE_ALT.some((r) => r.test(content));
}

export function messageIncomplet(messages: ChatMsg[], index: number, t: (s: string) => string): boolean {
  const m = messages[index];
  if (!m || m.role !== "assistant") return false;
  return fichierLaisseOuvert(messages, index, t) || abandonDeclare(m.content);
}


/** The edits of a message, with the result and any failures. */
export function appliquerEdits(messages: ChatMsg[], index: number, t: (s: string) => string): {
  fichiers: Artifact[];
  echecs: string[];
} {
  const edits = parseEdits(messages[index]?.content ?? "");
  if (!edits.length) return { fichiers: [], echecs: [] };
  const avant = fichiersJusqua(messages, index - 1, t);
  const apres = fichiersJusqua(messages, index, t);
  const echecs: string[] = [];
  const touches = new Map<string, Artifact>();
  for (const e of edits) {
    const cle =
      (avant.has(e.file) && e.file) ||
      [...avant.keys()].find((k) => k === e.file || k.endsWith("/" + e.file) || e.file.endsWith("/" + k));
    if (!cle) { echecs.push(e.file || "?"); continue; }
    if (!avant.get(cle)!.content.includes(e.find)) { echecs.push(cle); continue; }
    const f = apres.get(cle)!;
    touches.set(cle, { kind: "code", title: cle, lang: f.lang, content: f.content });
  }
  return { fichiers: [...touches.values()], echecs };
}
// Snippets / reusable prompts, saved by the user (browser).
export type Snippet = { id: string; label: string; content: string };

// A conversation tab: a snapshot of the live state when leaving it.
export type Tab = {
  id: string;
  title: string;
  currentId: string | null;
  model: string;
  settings: Settings;
  attachments: Attachment[];
  messages: ChatMsg[];
};

export type QueuedMsg = { content: string; text: string; attachmentCount?: number; images?: string[]; ts: number };
