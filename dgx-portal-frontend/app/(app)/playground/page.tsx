"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useToast } from "@astryxdesign/core/Toast";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Toolbar } from "@astryxdesign/core/Toolbar";
import { useResizable, ResizeHandle } from "@astryxdesign/core/Resizable";
import { Heading } from "@astryxdesign/core/Heading";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Selector } from "@astryxdesign/core/Selector";
// Wrapper that closes over the dependency's URL filter (see lib/markdown.tsx).
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { ReasoningBlock } from "../_components/ReasoningBlock";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Token } from "@astryxdesign/core/Token";
import { Thumbnail } from "@astryxdesign/core/Thumbnail";
import { Lightbox } from "@astryxdesign/core/Lightbox";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import {
  ChatLayout,
  ChatMessageList,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageMetadata,
  ChatComposer,
  ChatComposerDrawer,
  ChatComposerInput,
} from "@astryxdesign/core/Chat";
import StarSolidIcon from "@heroicons/react/24/solid/StarIcon";
import {
  PaperClipIcon,
  Cog6ToothIcon,
  ArrowDownTrayIcon,
  ArrowDownIcon,
  ClipboardDocumentIcon,
  StarIcon,
  BookmarkIcon,
  LinkIcon,
  ArrowPathIcon,
  CheckIcon,
  PencilIcon,
  PlusIcon,
  ClockIcon,
  TrashIcon,
  SparklesIcon,
  DocumentMagnifyingGlassIcon,
  DocumentTextIcon,
  XMarkIcon,
  PaperAirplaneIcon,
  ArrowUpIcon,
  StopIcon,
  KeyIcon,
  ArrowsPointingOutIcon,
  BoltIcon,
} from "@heroicons/react/24/outline";
import { useT, useLocale, tServeur } from "@/lib/i18n";
import { useWhoami } from "@/lib/whoami";
import { useCsrf } from "@/lib/useCsrf";
import { useSettingsDialog } from "@/lib/settings-dialog";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { DictateButton } from "../_components/DictateButton";
import { GenerationPlaceholder } from "../_components/GenerationPlaceholder";
import { BorderBeam } from "border-beam";
import { VoiceBeam } from "voice-glow";
import { useThemeMode } from "../../theme-provider";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { type EtapeWeb, fetchPlaygroundData, sendJSON, streamChat } from "@/lib/api";
// System notices (cronos_notice) are shared with the Support assistant,
// which also runs on the user's key: see lib/notices.ts.
import { texteNotice } from "@/lib/notices";
import { copierTexte } from "@/lib/copier";
import {
  convAsJson, convAsMarkdown, convTitleFallback, downloadText, type ExportConversation,
} from "@/lib/export";
// Pure model-output parsers, extracted for unit tests (tests/playground-parsers.test.ts).
import {
  parseAsk, parseEdits, estBlocQuestions, contenuCloture, corpsDeSuite, recoller, openCodeFence,
  reponseIncomplete,
} from "@/lib/playground-parsers";

import {
  fetchConversation,
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
import { AskQuestion } from "./_components/AskQuestion";
import { fmtK } from "./_components/ContextMeter";
import { ContextRing } from "./_components/ContextRing";
import { SettingsPanel } from "./_components/SettingsPanel";
import { SkillsMenu } from "./_components/SkillsMenu";
import { SkillCreator } from "./_components/SkillCreator";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";
import { BASE_SKILLS, type Skill, loadCustomSkills, saveCustomSkills, skillMatches } from "@/lib/skills";

const DEFAULT_SETTINGS: Settings = {
  system: "",
  temperature: 0.7,
  // The maximum. Only tokens ACTUALLY produced are billed, so a
  // high ceiling costs nothing on a short answer — and 4096 cleanly cut off any
  // slightly long answer (full HTML page, large configuration file).
  // The backend lowers this value to what remains in the context window
  // once the prompt is counted: a long conversation therefore does not fail,
  // it simply gets a shorter answer.
  maxTokens: 131072,
  topP: 1,
  reasoning: "auto" as const,
  // Reasoning effort: '' = leave the model's template at its default
  // value (recognized values depend on the model — e.g. Qwen3.8: xhigh).
  reasoningEffort: "",
};

const ATTACH_ACCEPT =
  ".md,.markdown,.txt,.text,.log,.logs,.err,.error,.out,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.py,.js,.ts,.jsx,.tsx,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.bash,.sql,.html,.css,.xml,.diff,.patch";

const MAX_ATTACHMENT_BYTES = 96 * 1024;

/** Extensions that the `accept` attribute only SUGGESTS: a file picker
 *  always lets « Tous les fichiers » through, and drag-and-drop
 *  ignores it entirely. Without this check, an image was read as TEXT
 *  (`readAsText`) and its binary content went into the prompt as
 *  mojibake, with no warning at all. */
const ATTACH_EXTENSIONS = ATTACH_ACCEPT.split(",").map((e) => e.trim().toLowerCase());

/** Images: accepted ONLY if the selected model reads them (`model_vision`).
 *  Downscaled in the browser before sending — a phone photo (4 000 px,
 *  5 Mo) would otherwise cost thousands of preload tokens and exceed
 *  the conversation save limit. 1 568 px is enough to read text
 *  or a screenshot. See `preparerImage`: an already reasonable image
 *  goes as is, without going through a canvas. */
const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif";
const IMAGE_EXTENSIONS = IMAGE_ACCEPT.split(",");
const IMAGE_COTE_MAX = 1568;
const IMAGE_SOURCE_MAX_BYTES = 25 * 1024 * 1024;
const IMAGES_PAR_MESSAGE = 4;
/** Images replayed to the model per request: the most recent ones (same bound as the server). */
const IMAGES_MAX_REQUETE = 8;
/** Image budget of a SAVED conversation (characters): the server bounds
 *  the conversation to 2 M and the form field to 4 Mo. Beyond that, the
 *  oldest images are no longer kept (the text, however, is). */
const IMAGES_BUDGET_SAUVEGARDE = 1_500_000;
/** Estimated weight of an image in the context window, in characters (~1 500 tokens). */
const IMAGE_POIDS_CHARS = 1500 * 4;

/** Size beyond which an image is downscaled (the server rejects > ~2,2 Mo). */
const IMAGE_ORIGINALE_MAX_BYTES = 2 * 1024 * 1024;

function lireDataUrl(file: Blob): Promise<string> {
  return new Promise((ok, ko) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => ko(r.error);
    r.readAsDataURL(file);
  });
}

/** Does the canvas really render what is drawn on it?
 *  Measured on 2026-10-01: on some browsers (canvas anti-tracking
 *  protections), reading back yields an EMPTY canvas — the image went out
 *  all black, and the model confidently described an invented scene. */
function canevasFiable(): boolean {
  try {
    const c = document.createElement("canvas");
    c.width = c.height = 4;
    const ctx = c.getContext("2d");
    if (!ctx) return false;
    ctx.fillStyle = "rgb(255,0,0)";
    ctx.fillRect(0, 0, 4, 4);
    const [r, g, b, a] = ctx.getImageData(1, 1, 1, 1).data;
    return r > 200 && g < 60 && b < 60 && a > 200;
  } catch {
    return false;
  }
}

/** The image to send, as a `data:` URL.
 *  An already reasonable image goes AS IS (no canvas: nothing to
 *  damage). Only a large image is downscaled to IMAGE_COTE_MAX in JPEG, and
 *  only if the canvas is reliable; otherwise we refuse rather than send
 *  a black image. */
async function preparerImage(file: File): Promise<string> {
  const bmp = await createImageBitmap(file);
  const { width, height } = bmp;
  const typeOk = /^image\/(png|jpeg|webp|gif)$/.test(file.type);
  if (typeOk && file.size <= IMAGE_ORIGINALE_MAX_BYTES && Math.max(width, height) <= IMAGE_COTE_MAX * 2) {
    bmp.close();
    return lireDataUrl(file);
  }
  if (!canevasFiable()) {
    bmp.close();
    throw new Error("canevas");
  }
  const echelle = Math.min(1, IMAGE_COTE_MAX / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * echelle));
  canvas.height = Math.max(1, Math.round(height * echelle));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas");
  ctx.fillStyle = "white";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  return canvas.toDataURL("image/jpeg", 0.85);
}

/** Keeps images only on the most recent messages, within `max`
 *  images or `budget` characters. Text is never touched. */
function imagesRecentes<M extends { images?: string[] }>(msgs: M[], max: number, budget = Infinity): M[] {
  let reste = max;
  let poids = 0;
  const out = [...msgs];
  for (let i = out.length - 1; i >= 0; i--) {
    const imgs = out[i].images;
    if (!imgs?.length) continue;
    const garde: string[] = [];
    for (let k = imgs.length - 1; k >= 0 && reste > 0; k--) {
      if (poids + imgs[k].length > budget) { reste = 0; break; }
      garde.unshift(imgs[k]);
      poids += imgs[k].length;
      reste--;
    }
    out[i] = { ...out[i], images: garde.length ? garde : undefined };
  }
  return out;
}

/** Title and summary only read the text: no need to ship megabytes of images through them. */
function sansImages(msgs: ChatMsg[]) {
  return msgs.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden }));
}

/** Messages as they are saved (flags + images within the budget). */
function pourSauvegarde(msgs: ChatMsg[]) {
  return imagesRecentes(
    msgs.map((m) => ({
      role: m.role, content: m.content, hidden: m.hidden,
      truncated: m.truncated, isError: m.isError, images: m.images,
    })),
    Infinity, IMAGES_BUDGET_SAUVEGARDE);
}

// Something the assistant produced worth showing in the side panel (canvas/
// artifact style) and copying in one click: a code "file" or a long "document"
// (e.g. a rewritten/reformatted text).
type Artifact =
  | { kind: "code"; title: string; lang: string; content: string }
  | { kind: "doc"; title: string; content: string };

// Below this length a document-task answer is probably a clarifying question →
// keep it inline rather than filing it as a document.
const DOC_MIN_CHARS = 400;

// Appended to the system prompt so the model can ask the user one or several
// multiple-choice clarifying questions (rendered as selectable answers, submitted
// together) instead of guessing — the same idea as Claude's "ask the user" tool.
const ASK_INSTRUCTION = `When you need the user to clarify things before you can answer well, ask your questions as a single fenced block. Output it exactly like this:
\`\`\`ask
{"questions": [{"question": "<question 1>", "options": ["<option>", "<option>"]}, {"question": "<question 2>", "options": ["<option>", "<option>", "<option>"]}]}
\`\`\`
Strict rules:
- Before the block you MAY write ONE short introductory sentence (e.g. "Bien sûr ! Quelques précisions pour bien t'aider :"). Do NOT write the questions or their options as normal text anywhere — they go INSIDE the block ONLY.
- Ask as many questions as are genuinely useful — two if two are enough, more if the request really needs it. Do not pad to reach a number, and do not drop a question that matters. Each question gets 2 to 6 short options in the user's language. Ask everything you need in this one block (the user answers them all at once).
- Do NOT add an "Other" option (the interface adds one).
- The user can pick SEVERAL options for the same question, so write options that can be combined rather than mutually exclusive ones whenever that makes sense. Their answer may come back as "A + B".
- Ask AT MOST ONCE. As soon as the user has answered, you MUST give your real, complete answer using their choices — NEVER reply with another ask block once they have answered.
- The fence language MUST be \`ask\` — never \`json\` or anything else, or the user will not see clickable questions.
- Only ask when it genuinely helps; otherwise just answer normally.`;

// Instruction added to the system prompt: fix an already produced file without
// rewriting it entirely. Rewriting 400 lines to change three costs time,
// tokens, and reintroduces errors elsewhere in the file.
// The model names its own files: it knows what the user asked for
// and which project they fit into. Without this, the UI has to guess and
// falls back to a generic name.
const NAME_INSTRUCTION = `Name every file you output: put its path in backticks on the line just before the code block (\`index.html\`, \`roles/web/tasks/main.yml\`), chosen from what the user asked. Reuse the exact same name for a file you already produced.`;

// The partial-edit protocol was removed: on generated code containing
// errors, a spot fix left a half-right file, and
// a mis-copied anchor did not apply at all. We ask for the file
// ENTIRE — it is longer to generate, but what comes out is usable as is.
const REWRITE_INSTRUCTION = `When the user asks you to fix or change a file you already produced, output that file COMPLETE, from its first line to its last, under the exact same name. Never output a partial file, an excerpt, a diff, or a "rest unchanged" placeholder.`;

// The model GIVES UP on its own on a large file: measured in prod on 22/08,
// it stopped at 14 187 tokens out of 131 072 available, with
// finish_reason=stop (so nothing distinguished it from a successful answer),
// writing « Le fichier est trop long pour etre affiche en entier ici » followed
// by a truncated file presented as complete. No technical limit was
// reached. This instruction goes out on EVERY turn, not only when a
// file already exists, and goes LAST so as not to dilute ASK_INSTRUCTION
// (whose effectiveness depends on its position at the top, see measurement below).
const INTEGRALITE_INSTRUCTION = `Never abridge a file you were asked to produce. Never write that a file is "too long to show here", never say you are giving a "shortened", "simplified" or "essential" version, and never replace any part of a file with an ellipsis, a placeholder, or a comment such as "rest of the code unchanged". There is no display limit: write the file in full, from its first line to its last. If you run out of room before the end, stop mid-file rather than closing it early — you will be asked to continue, and you will resume at the exact character where you stopped. A truncated file presented as complete is the worst possible answer.`;

// Input placeholder: we rotate a few texts (including the « / » trick
// to call a skill). Each entry is an i18n key (FR-as-msgid).
const PLACEHOLDER_TEXTS = [
  "Comment puis-je vous aider aujourd'hui ?",
  "Tapez / pour appeler une compétence",
  "Résumez un document, générez une image, écrivez du code…",
];

// « / » commands that open the skill creator.
const SLASH_CREATE_COMMANDS = ["skill-creator", "create", "new", "creer", "competence", "compétence"];

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
function profondeurFinale(code: string): number {
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

const memoScript = new Map<string, boolean>();

function scriptCasse(contenu: string): boolean {
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
function libelleEtapeWeb(
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
function tourAvorte(m: ChatMsg | undefined): boolean {
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

const PROMPT_REPRISE = "Continue exactement là où tu t'es arrêté";
const PROMPT_REPRISE_COMPLET =
  PROMPT_REPRISE + ", sans rien répéter et sans réintroduire ta réponse. "
  + "Reprends au caractère suivant, et va jusqu'au bout du fichier.";
const PROMPT_INTEGRAL =
  "Tu viens d'abreger ce fichier alors que je l'ai demande complet. "
  + "Reecris-le en ENTIER, de la premiere a la derniere ligne, sans aucune coupure, "
  + "sans resume et sans « reste inchange ». Il n'y a aucune limite d'affichage.";
const REPRISE_INSTRUCTION = `Your previous reply was cut off in the middle of a file. Output ONLY the missing remainder of that file, starting at the exact character where you stopped. Do not repeat anything already written, do not re-introduce, do not summarise, and do not use an edit block — just continue the raw content until the file is complete.`;

/** Consecutive automatic resumes before handing back to the user. */
// Raised from 3 to 10: a single large file takes about ten segments
// (measured: 2 400 to 14 000 tokens per resume), and at 3 the chain still
// handed back on an unfinished file. Still bounded: a resume
// that loops without making progress must return to the user, not run forever.
const MAX_REPRISES_AUTO = 10;

/** Is this hidden message the resume request issued by « Continuer » ? */
function estReprise(m: ChatMsg | undefined): boolean {
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
function trimAfterAsk(content: string): string {
  const i = content.indexOf("```ask");
  if (i < 0) return content;
  const close = content.indexOf("```", i + 6);
  return close < 0 ? content : content.slice(0, close + 3);
}

// Whether the user's request is a "document" task (correct / rewrite / reformat /
// draft / "make a document/report/note…"). Only then is the answer treated as a
// document. Accent- and language-insensitive (FR + EN). Plain "explain"/"summarize"
// stays inline.
function isDocTask(prompt: string): boolean {
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
function docTitleFromContent(content: string): string {
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
function slugify(s: string): string {
  const base = s
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "") // strip accents
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || "document";
}

// Pinned (starred) conversations: a personal preference stored in the browser —
// no backend migration, shared on this machine.
const PINNED_KEY = "cronos.pinned.conversations";
function loadPinnedIds(): string[] {
  try {
    return (JSON.parse(localStorage.getItem(PINNED_KEY) || "[]") as string[]);
  } catch {
    return [];
  }
}
function savePinnedIds(ids: string[]) {
  try {
    localStorage.setItem(PINNED_KEY, JSON.stringify(ids));
  } catch {
    /* stockage indisponible (mode privé) : on ignore */
  }
}

// Math/LaTeX: rendering now lives in `lib/maths.tsx`, called by
// `MarkdownSur` (see `lib/markdown.tsx`). It could not stay here: an
// `inlinePlugins` applies PER text node, and the parser splits text at
// every backslash — so the formula was never recognized. Protecting
// LaTeX now happens BEFORE the parser.
// Snippets / reusable prompts, saved by the user (browser).
type Snippet = { id: string; label: string; content: string };
const SNIPPET_KEY = "cronos.snippets";
function loadSnippets(): Snippet[] {
  try {
    return (JSON.parse(localStorage.getItem(SNIPPET_KEY) || "[]") as Snippet[]);
  } catch {
    return [];
  }
}
function saveSnippets(list: Snippet[]) {
  try {
    localStorage.setItem(SNIPPET_KEY, JSON.stringify(list));
  } catch {
    /* stockage indisponible : on ignore */
  }
}

// Tabs: several conversations open in parallel. The generation logic
// stays intact (the live state `messages`/`model`/… belongs to the ACTIVE
// tab); we only save/restore each tab's snapshot on
// switch. Switching is disabled during a stream so as not to cut a
// answer in progress.
type Tab = {
  id: string;
  title: string;
  currentId: string | null;
  model: string;
  settings: Settings;
  attachments: Attachment[];
  messages: ChatMsg[];
};
let tabSeq = 0;
function newTabId() {
  tabSeq += 1;
  return `tab-${Date.now()}-${tabSeq}`;
}

// Renaming generated files (artifacts). Key = `convId::kind::titre`
// (file names are made unique by the model) and persisted in
// localStorage to survive a reload; `convId` prevents the same name
// (`fichier-1.yml`) in two conversations from renaming each other.
const ARTIFACT_RENAME_KEY = "cronos.artifact.renames";
function loadArtifactRenames(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(ARTIFACT_RENAME_KEY) || "{}") as Record<string, string>;
  } catch {
    return {};
  }
}
function saveArtifactRenames(m: Record<string, string>) {
  try {
    localStorage.setItem(ARTIFACT_RENAME_KEY, JSON.stringify(m));
  } catch {
    /* stockage indisponible : on ignore */
  }
}
function artifactRenameKey(convId: string | null, a: Artifact): string {
  return `${convId ?? "anon"}::${a.kind}::${a.title}`;
}

// Playground settings (persona, generation, reasoning): a personal preference
// kept in the browser, like the pinned conversations and the snippets.
// « je dois le sélectionner à chaque fois » : NOTHING was saved, so every page
// load — and every round trip through another page — reset the panel to
// DEFAULT_SETTINGS, the reasoning choice (Auto / Toujours / Jamais) first. Now
// loaded ONCE at mount, saved on every change. Per browser, like every other
// playground preference: a server-side copy would follow the account, this one
// needs no migration and restores without a single loading frame.
const SETTINGS_KEY = "cronos.playground.settings";
type Provenance = "persona" | "skill" | "manual";
type ReglagesStockes = { settings: Settings; provenance: Provenance };
function chargerReglages(): ReglagesStockes {
  const base: ReglagesStockes = { settings: DEFAULT_SETTINGS, provenance: "manual" };
  try {
    const brut = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null") as
      (Partial<Settings> & { provenance?: unknown }) | null;
    if (!brut || typeof brut !== "object") return base;
    // Field by field over the defaults: a value stored by an OLDER version (or
    // hand-edited) must not break the panel — unknown or wrong-typed, it is
    // ignored and the default stands.
    return {
      settings: {
        ...DEFAULT_SETTINGS,
        system: typeof brut.system === "string" ? brut.system : DEFAULT_SETTINGS.system,
        temperature: typeof brut.temperature === "number" ? brut.temperature : DEFAULT_SETTINGS.temperature,
        maxTokens: typeof brut.maxTokens === "number" ? brut.maxTokens : DEFAULT_SETTINGS.maxTokens,
        topP: typeof brut.topP === "number" ? brut.topP : DEFAULT_SETTINGS.topP,
        reasoning:
          brut.reasoning === true || brut.reasoning === false || brut.reasoning === "auto"
            ? brut.reasoning
            : DEFAULT_SETTINGS.reasoning,
        reasoningEffort:
          typeof brut.reasoningEffort === "string" ? brut.reasoningEffort : DEFAULT_SETTINGS.reasoningEffort,
      },
      provenance: brut.provenance === "persona" ? "persona" : "manual",
    };
  } catch {
    return base; // stockage illisible : on repart des réglages par défaut
  }
}
function enregistrerReglages(settings: Settings, provenance: Provenance) {
  try {
    // The system prompt a SKILL injects belongs to that skill, not to the
    // user's own defaults: selecting a skill must not become the persona of
    // every conversation to come. We keep the stored one instead.
    const ancien = chargerReglages();
    const persona =
      provenance === "skill"
        ? { system: ancien.settings.system, provenance: ancien.provenance }
        : { system: settings.system, provenance };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...settings, ...persona }));
  } catch {
    /* stockage indisponible (mode privé) : on ignore */
  }
}

// Extensions recognized as FILES. Deliberately a closed list: without
// it, « ansible.builtin.reboot » or « os_family['debian'] » would pass for
// file names and produce absurd titles.
const FILE_EXT = new RegExp(
  "\\.(ya?ml|json|jsonc|toml|ini|cfg|conf|env|py|js|mjs|cjs|ts|tsx|jsx|sh|bash|zsh|" +
  "go|rs|rb|php|java|kt|c|h|cpp|sql|html|css|scss|md|txt|log|xml|service|tf|gradle)$",
  "i");
const BARE_FILES = /^(Dockerfile|Makefile|Vagrantfile|Jenkinsfile|Procfile)$/i;

/** File name announced right BEFORE a code block.
 *
 * A model almost always writes « ### 2. `tasks/main.yml` » then the block.
 * Without reading this context, artifacts were named « file 1 », « yaml · 2 »… —
 * cards one could no longer tell which file they belonged to,
 * while the prose just above did name the files.
 */
function titleFromContext(before: string): string {
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
function parseArtifacts(content: string, allowDoc: boolean, t: (s: string) => string): { prose: string; artifacts: Artifact[] } {
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
      // Nom de repli traduit : un fichier téléchargé ne doit pas garder un nom
      // français en mode anglais.
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
const LANG_INFO: Record<string, { ext: string; mime: string }> = {
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
function nomTelechargeable(titre: string, lang: string): string {
  if (/\.[a-z0-9]{1,6}$/i.test(titre)) return titre;
  const info = LANG_INFO[(lang || "").toLowerCase()];
  const base = titre.replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "fichier";
  return info ? `${base}.${info.ext}` : `${base}.txt`;
}

function mimePourLangage(lang: string): string {
  return LANG_INFO[(lang || "").toLowerCase()]?.mime ?? "text/plain";
}

/** Is this block a snippet of an already known file, rather than a version? */
function estFragment(
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
const TITRE_GENERIQUE = /^(?:fichier|file)-\d+\.[a-z0-9]{1,6}$/i;

/** The files for which THIS message contains only a snippet.
 *
 * Two forms, both seen in the wild:
 *  - the block carries the file name but is much shorter → named snippet;
 *  - the block is not named at all (« voici la partie corrigée ») while a
 *    much larger file of the same language already exists → anonymous snippet.
 */
function fragmentsDuMessage(messages: ChatMsg[], index: number, t: (s: string) => string): string[] {
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
function fichiersJusqua(
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
function fusionDuMessage(messages: ChatMsg[], index: number, t: (s: string) => string): Artifact[] {
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
function fichierLaisseOuvert(messages: ChatMsg[], index: number, t: (s: string) => string): boolean {
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
const ABANDON_DECLARE =
  /trop\s+(?:long|volumineux|gros)[^.\n]{0,80}?(?:affich|ici\b|ce\s+message)/i;
const ABANDON_DECLARE_ALT = [
  /too\s+(?:long|large)[^.\n]{0,80}?(?:display|show\b|here\b|message)/i,
  /je\s+ne\s+peux\s+pas\s+(?:l['’]?)?(?:affich|[ée]crire)[^.\n]{0,60}?(?:int[ée]gralit|en\s+entier)/i,
  /(?:reste|suite)\s+du\s+(?:code|fichier)\s+(?:inchang|identique|omis)/i,
  /rest\s+of\s+the\s+(?:code|file)[^.\n]{0,30}?unchanged/i,
  /\.\.\.\s*\(\s*(?:suite|reste)/i,
];

function abandonDeclare(content: string): boolean {
  // Only on a message that claims to deliver code: the same sentence in
  // a prose answer signals nothing to resume.
  if (!content.includes("```")) return false;
  return ABANDON_DECLARE.test(content) || ABANDON_DECLARE_ALT.some((r) => r.test(content));
}

function messageIncomplet(messages: ChatMsg[], index: number, t: (s: string) => string): boolean {
  const m = messages[index];
  if (!m || m.role !== "assistant") return false;
  return fichierLaisseOuvert(messages, index, t) || abandonDeclare(m.content);
}


/** The edits of a message, with the result and any failures. */
function appliquerEdits(messages: ChatMsg[], index: number, t: (s: string) => string): {
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

type QueuedMsg = { content: string; text: string; attachmentCount?: number; images?: string[]; ts: number };

// Playground settings panel: target width, and safety margin to the
// window edge (= --spacing-2). See toggleSettings for the clamping.
const LARGEUR_PANNEAU_REGLAGES = 480;
const MARGE_PANNEAU_REGLAGES = 8;

export default function PlaygroundPage() {
  const t = useT();
  const numLocale = useLocale();
  // The composer's two effects (border beam, voice halo) paint
  // according to the background: we give them the APPLICATION mode, not `auto`,
  // because Astryx only sets `data-theme` for dark (see ThinkingIndicator).
  const { mode } = useThemeMode();
  const { open: openSettings } = useSettingsDialog();
  const csrf = useCsrf();
  const [runningModels, setRunningModels] = useState<string[]>([]);
  const [modelLimits, setModelLimits] = useState<Record<string, number>>({});
  const [modelVision, setModelVision] = useState<Record<string, boolean>>({});
  // Image opened large from a bubble (null = closed).
  const [imageVue, setImageVue] = useState<{ srcs: string[]; index: number } | null>(null);
  // null = not known yet. We only show the warning once the answer is received,
  // so as not to flash a warning on load.
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  // Index of the user message being edited (inline edit). The
  // conversation stays displayed; on resend, we rebranch from that index.
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // Playground settings, restored from the browser (see `chargerReglages`):
  // « je dois le sélectionner à chaque fois » — nothing was kept, so every
  // reload threw the panel back to the defaults. Both states read the SAME
  // stored object (parsed once), and every change writes it back.
  const [reglagesDepart] = useState(chargerReglages);
  const [settings, setSettings] = useState<Settings>(reglagesDepart.settings);
  // Origin of the system prompt: a persona, a skill, or manual input.
  // Allows flagging that a skill overwrote a persona's system prompt.
  const [systemProvenance, setSystemProvenance] = useState<Provenance>(reglagesDepart.provenance);
  useEffect(() => {
    enregistrerReglages(settings, systemProvenance);
  }, [settings, systemProvenance]);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Position of the settings panel: captured on the button click (button rect →
  // bottom-left corner of the panel under the gear). FIXED overlay:
  // it never touches the flow, so the page does not move a pixel.
  const [settingsPos, setSettingsPos] = useState<{ top: number; right: number; maxH: number; largeur: number } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Closing the settings panel: outside click or Escape. NO focus move
  // on open — the Popover component's focus trap used to
  // scroll the page (sr-only close button revealed under the panel),
  // hence a chat "yanked back down" on every click of the gear.
  const toggleSettings = (el: HTMLElement | null) => {
    if (!isSettingsOpen && el) {
      const r = el.getBoundingClientRect();
      // The panel is anchored to the gear, but its width cannot exceed the
      // window: under 768 px the sidebar is folded, the gear moves down to
      // ~250 px from the left edge, and a 480 px panel anchored there started 201 px
      // OFF SCREEN — with no way out, a `position: fixed` does not scroll. So we clamp
      // the width to the window, then the right offset so the left
      // edge stays visible. Beyond ~1024 px the gear is far enough right:
      // both bounds are inactive and the panel keeps its nominal size.
      const largeur = Math.min(LARGEUR_PANNEAU_REGLAGES, window.innerWidth - 2 * MARGE_PANNEAU_REGLAGES);
      setSettingsPos({
        top: r.bottom + 8,
        right: Math.max(MARGE_PANNEAU_REGLAGES,
          Math.min(window.innerWidth - r.right, window.innerWidth - largeur - MARGE_PANNEAU_REGLAGES)),
        maxH: window.innerHeight - r.bottom - 48,
        largeur,
      });
    }
    setIsSettingsOpen((v) => !v);
  };
  useEffect(() => {
    if (!isSettingsOpen) return;
    const onDown = (e: PointerEvent) => {
      const cible = e.target as HTMLElement | null;
      // Clicking the gear itself goes through the button's toggle: do not
      // close here, otherwise pointerdown closes then click reopens (net zero).
      if (cible?.closest(".playground-settings-panel")) return;
      setIsSettingsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsSettingsOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [isSettingsOpen]);
  // Search in history + pinned (starred) conversations.
  const [histQuery, setHistQuery] = useState("");
  const [pinnedIds, setPinnedIds] = useState<string[]>(loadPinnedIds);
  // Renaming a conversation from the history.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [streaming, setStreaming] = useState(false);

  // Tabs: several conversations open. The live state above is
  // the active tab; each `tabs` entry keeps a snapshot of it.
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activeTabId, setActiveTabId] = useState<string>("");
  const tabsRef = useRef<Tab[]>([]);
  useEffect(() => { tabsRef.current = tabs; }, [tabs]);
  const tabsInitRef = useRef(false);
  useEffect(() => {
    if (tabsInitRef.current) return;
    tabsInitRef.current = true;
    const id = newTabId();
    setActiveTabId(id);
    setTabs([{ id, title: "", currentId: null, model, settings, attachments: [], messages: [] }]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Queue: messages submitted while an answer is generating. Instead
  // of being lost (the old « if (streaming) return »), they pile up in a
  // panel above the composer and go out ALL BY THEMSELVES as soon as the current
  // answer ends. Each row's buttons only short-circuit
  // this wait: « Envoyer » interrupts the current answer (its already
  // written part is kept, as with Stop) to move to this message right away.
  // Chained resumes without intervention: beyond that, we hand back rather
  // than let a looping model consume the account's budget.
  const reprisesRef = useRef(0);
  const [reprise, setReprise] = useState(0);
  const [queued, setQueued] = useState<QueuedMsg[]>([]);
  const queuedRef = useRef<QueuedMsg[]>([]);
  // runStream closes the conversation from its own closure: to start again from
  // the up-to-date list (partial answer kept, etc.) we read via ref.
  const messagesRef = useRef(messages);
  useEffect(() => { messagesRef.current = messages; });

  // ── Thread auto-scroll during generation ───────────────────────────────────
  // Suivre le bas du flux pendant qu'il pousse — et S'ARRÊTER dès que le
  // lecteur remonte. Le mécanisme d'avant posait son écouteur au MONTAGE de
  // la page (deps []) sur `.astryx-chat-layout` : or ChatLayout n'existe pas
  // encore à ce moment-là — il ne s'affiche qu'à la première conversation
  // (ternaire `isFirstEmpty`) — donc l'écouteur n'était JAMAIS posé,
  // `suitLeBas` restait figé à true et l'épinglage ramenait en bas à CHAQUE
  // rendu : « je ne peux plus monter quand il réfléchit ». Le hook maison
  // (useStickToBottom, déjà en service pour le panneau live et les logs
  // admin) s'attache par REF : il se pose quand l'élément apparaît, et se
  // réarme sur la vraie distance au bas (48 px), pas sur un delta de scroll.
  const dernier = messages[messages.length - 1];
  const fluxDep = `${dernier?.content?.length ?? 0}:${dernier?.reasoning?.length ?? 0}`;
  const {
    setRef: suitLeBasSetRef,
    showButton: montrerDescendre,
    scrollToBottom: descendreToutEnBas,
  } = useStickToBottom(fluxDep, streaming);
  // Le conteneur de défilement est la RACINE de ChatLayout, et elle seule.
  // Cause racine du « la scrollbar ne marche pas » (2026-10-04) : on passait
  // `scrollRef` à ChatLayout avec un ref JAMAIS rattaché à un élément —
  // ChatLayout cesse alors d'être auto-défilant (`isSelfScrolling = !scrollRef`)
  // et rend sa racine en `overflow: visible` : AUCUN conteneur de défilement
  // n'existait dans la colonne de chat, ni la molette ni la barre ne faisaient
  // quoi que ce soit (mesure : `scrollHeight` 1278 pour `clientHeight` 817 et
  // `scrollTop` toujours 0). Ici `suitLeBasSetRef` EST le ref de rappel de la
  // racine : il s'attache quand la conversation apparaît (ChatLayout n'existe
  // pas sur la page vide), et sert au suivi du bas comme au bouton « Descendre ».

  const updateQueue = (q: QueuedMsg[]) => {
    queuedRef.current = q;
    setQueued(q);
  };
  // The queue panel grows/shrinks the dock, which is sticky at the BOTTOM of the
  // scroller: its height adds to the content, and if we were following the bottom,
  // the last line ends up hidden underneath. Astryx only observes the message
  // content, not the dock — hence this explicit re-stick. We only do it if
  // we were already stuck to the bottom, so as not to yank someone re-reading
  // further up. The scroller is ChatLayout itself (no scrollRef provided),
  // and the page contains only one.
  useEffect(() => {
    const el = document.querySelector<HTMLElement>(".astryx-chat-layout");
    if (!el) return;
    // Wide margin: the gap just GREW by the panel's height.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [queued.length]);
  // Live rate: `usage.completion_tokens` only arrives at the END of the stream. We
  // therefore estimate the token count from the received CHARACTERS — counting
  // SSE deltas would be wrong (measured: with MTP speculative decoding, vLLM sends
  // several tokens per delta, hence ~2,7x underestimation). The
  // characters/token ratio is auto-calibrated at the end of each generation on the
  // exact `usage`, so it adapts to the model and the language (measured: ~4,5 in
  // French, ~5,0 in English). Refs: updated on every delta without re-render.
  const liveCharsRef = useRef(0);
  const charsPerTokenRef = useRef(4.8);
  const liveStartRef = useRef<number | null>(null);
  const [liveStats, setLiveStats] = useState<{ tokens: number; tps: number } | null>(null);

  // Refreshes the displayed counter 4x/s during the stream — smooth enough to the eye,
  // without adding a re-render per token (updateLast already does one).
  useEffect(() => {
    if (!streaming) return;
    const id = setInterval(() => {
      const start = liveStartRef.current;
      const chars = liveCharsRef.current;
      if (!start || !chars) return;
      const secs = (performance.now() - start) / 1000;
      const tokens = Math.round(chars / charsPerTokenRef.current);
      if (secs > 0 && tokens > 0) setLiveStats({ tokens, tps: Number((tokens / secs).toFixed(1)) });
    }, 250);
    return () => clearInterval(id);
  }, [streaming]);
  // Web-search steps of the IN-PROGRESS generation. Without this display,
  // the wait is silent: several tens of seconds during which the
  // model searches and reads, with nothing indicating it.
  const [etapesWeb, setEtapesWeb] = useState<EtapeWeb[]>([]);
  // First runtime error reported by the preview (empty = the page runs).
  const [erreurApercu, setErreurApercu] = useState("");
  const [currentId, setCurrentId] = useState<string | null>(null);
  // Mirror of `currentId`: the end of a stream runs in its own closure and
  // must know whether the user is STILL looking at the same conversation (see
  // below, "did the thread change during the generation").
  const currentIdRef = useRef<string | null>(null);
  useEffect(() => { currentIdRef.current = currentId; }, [currentId]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [ctxUsed, setCtxUsed] = useState(0);
  // Input/output split of the LAST generation (exact measurement reported
  // by LiteLLM via `usage`): prompt_tokens = what fills the window,
  // completion_tokens = the produced answer. null until a generation happens.
  const [ioTokens, setIoTokens] = useState<{ prompt: number; completion: number } | null>(null);
  // Cumulated tokens of the current conversation (real cost: each request
  // bills prompt + completion, so the sum of `total_tokens` = what is
  // deducted from the budget). Per message, `m.tokens` is the already existing display.
  const [convTokens, setConvTokens] = useState(0);
  useWhoami();
  // Artifact/canvas side-panel: when the assistant writes a file (a substantial
  // code block), it opens on the side automatically instead of being dumped
  // inline — inspired by the Astryx ai-chat template.
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const artifactResize = useResizable({ defaultSize: 560, minSizePx: 420, maxSizePx: 900, autoSaveId: "playground-artifact" });
  // On phones the resizable side panel would crush the chat, so the artifact
  // opens as a fullscreen dialog instead.
  const isNarrow = useIsNarrow();
  // "Watch the document being written live": set when the user clicks the
  // in-progress document card during a document stream. A ref mirrors it so the
  // stream-completion closure can read the current value.
  const [liveDocOpen, setLiveDocOpen] = useState(false);
  // Rendered preview of a generated HTML page, rather than its source code.
  const [htmlPreview, setHtmlPreview] = useState(true);
  // URL of the preview served by the backend. A `srcdoc` iframe inherits the
  // portal's CSP and its inline scripts are blocked: the page displays but
  // nothing responds in it. So we go through a response that carries its own sandbox.
  const [previewUrl, setPreviewUrl] = useState("");
  // Full screen for the panel: essential to watch a generated HTML page,
  // unreadable in a 400 px column.
  const [plein, setPlein] = useState(false);
  const liveDocOpenRef = useRef(false);
  /** Panel close: leaving full screen returns to the side panel, we only
   *  close the file if we were not in it. */
  const fermerPanneau = () => {
    if (plein) { setPlein(false); return; }
    setArtifact(null);
    setLiveDocOpen(false);
    // Un volet refermé le reste : la rédaction en cours ne doit pas le rouvrir
    // derrière l'utilisateur (le ref sert au bilan de fin de flux).
    liveDocOpenRef.current = false;
  };
  const openLiveDoc = () => { setLiveDocOpen(true); liveDocOpenRef.current = true; };

  const abortRef = useRef<AbortController | null>(null);

  // Dictation: same hook as the Voice and Video pages.
  const dictation = useDictation({ value: input, onChange: setInput, csrf });
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Radius of the composer card, in pixels, for the two effects that
  // wrap it (voice halo, border beam).
  //
  // Both libraries can detect it on their own — but on their
  // FIRST CHILD, and `border-beam`'s first child is its OWN
  // `<style>` tag (0 px): measured on 2026-10-01, the beam was therefore drawn with
  // the 16 px fallback in a 28 card, corners visibly squarer than
  // the composer. We read the token rather than hardcoding « 28 » here: it is the
  // same source of truth as the card (`--radius-chat`, `:root` in
  // astryx.css), so a theme that changes radii follows.
  const [rayonComposeur, setRayonComposeur] = useState<number | undefined>(undefined);
  useEffect(() => {
    const px = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue("--radius-chat"));
    // DOM read on mount: `getComputedStyle` does not exist at server
    // render, so the value cannot be derived during render. A single
    // setState, once, no cascade — this is the "sync
    // from an external system" case the rule aims to distinguish from render
    // loops. (The directive must IMMEDIATELY precede the code: placed before the
    // comments, it only disabled the first comment line.)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (Number.isFinite(px) && px > 0) setRayonComposeur(px);
  }, []);

  useEffect(() => {
    // A WAITING guard, not a dead button: the effect replays as soon as the token
    // arrives (`csrf` dependency), so nothing to report to the user.
    if (!csrf) return;
    let annule = false;
    (async () => {
      await migrateLegacyConversations(csrf);
      const convs = await fetchConversations();
      if (!annule) setConversations(convs);
    })();
    return () => {
      annule = true;
    };
  }, [csrf]);

  useEffect(() => {
    fetchPlaygroundData()
      .then((data) => {
        setRunningModels(data.running_models);
        setModelLimits(data.model_limits);
        setModelVision(data.model_vision ?? {});
        setHasKey(data.has_key);
        if (data.running_models.length) setModel(data.running_models[0]);
      })
      .catch(() => {});
  }, []);

  // Targeted open from the home page: ?conv=<id> opens a specific
  // conversation, ?model=<name> preselects the current model. Applied once,
  // once the conversations AND the current models are loaded (the two
  // fetches are in separate effects, the order is not guaranteed).
  const initFromUrlRef = useRef(false);
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps -- one-shot init from the URL */
  useEffect(() => {
    if (initFromUrlRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const convId = params.get("conv");
    const modelName = params.get("model");
    if (!convId && !modelName) return;
    if (runningModels.length === 0) return; // waiting for the models
    if (convId && conversations.length === 0) return; // waiting for the conversations
    initFromUrlRef.current = true;
    if (modelName && runningModels.includes(modelName)) setModel(modelName);
    const conv = convId ? conversations.find((c) => c.id === convId) : undefined;
    if (conv) {
      setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
      setCurrentId(conv.id);
      setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, title: conv.title, currentId: conv.id } : t)));
      if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
      setCtxUsed(0);
      setConvTokens(0);
      setIoTokens(null);
    }
  }, [conversations, runningModels]);
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  // persist()/runStream() only ever run from event handlers (send/regenerate/edit),
  // never during render, so Date.now()/performance.now() here are safe despite the
  // purity lint rule's conservative render-reachability analysis.
  function persist(msgs: ChatMsg[], convId: string | null, activeModel: string) {
    if (!msgs.length) return convId;
    const title = (msgs.find((m) => m.role === "user")?.content || t("Conversation")).slice(0, 80);
    const item: Conversation = {
      // eslint-disable-next-line react-hooks/purity
      id: convId ?? String(Date.now()),
      title,
      // eslint-disable-next-line react-hooks/purity
      ts: Date.now(),
      model: activeModel,
      // `hidden` is part of the message: without it, an answer to questions
      // became an ordinary message again on reload, shifting indexes and
      // changing the rendering of the conversation.
      // `truncated` and `isError` travel too, and the server keeps them:
      // without them, an answer cut mid-file passed for COMPLETE
      // after a reload — no more banner, no more « Continuer » button,
      // while the file remained unfinished on screen.
      messages: pourSauvegarde(msgs),
    };
    // Optimistic on the UI side, then server save in the background: the
    // list must not wait for the network round-trip to update.
    setConversations((prev) => {
      const rest = prev.filter((c) => c.id !== item.id);
      return [item, ...rest];
    });
    // A refused save (413, database unavailable) must not turn into
    // a silently emptied history: we SAY it.
    if (csrf) {
      void persistConversation(csrf, item).then((ok) => {
        if (!ok) showToast({ body: t("Conversation non enregistrée — elle disparaîtra au rechargement."), type: "error" });
      });
    }
    return item.id;
  }

  // Close any open document/file panel — it belongs to the conversation we're
  // leaving, not the one we're opening.
  function closeArtifact() {
    setArtifact(null);
    setLiveDocOpen(false);
  }

  function newConversation() {
    // No re-save here: each generation already saves at its end.
    // Re-saving used to move the conversation to the top of the list on the mere
    // fact of changing it, while the order must reflect the last ACTIVITY.
    // With tabs, « nouvelle conversation » opens a clean tab.
    newTab();
  }

  // Switch to a tab: we freeze the active tab's snapshot then
  // restore the target's. Disabled during a stream (the live state then
  // belongs to the ongoing generation).
  function switchTab(id: string) {
    if (streaming || id === activeTabId || !id) return;
    setTabs((prev) => prev.map((t) =>
      t.id === activeTabId ? { ...t, messages, currentId, model, settings, attachments } : t));
    const target = tabsRef.current.find((t) => t.id === id);
    if (!target) return;
    setMessages(target.messages);
    setCurrentId(target.currentId);
    if (target.model && runningModels.includes(target.model)) setModel(target.model);
    setSettings(target.settings);
    setAttachments(target.attachments);
    setCtxUsed(0);
    setConvTokens(0);
    setEditingIdx(null);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
    setActiveTabId(id);
  }

  function newTab() {
    if (streaming) return;
    const id = newTabId();
    setTabs((prev) => [
      ...prev.map((t) => (t.id === activeTabId ? { ...t, messages, currentId, model, settings, attachments } : t)),
      { id, title: "", currentId: null, model, settings, attachments: [], messages: [] },
    ]);
    setActiveTabId(id);
    setMessages([]);
    setCurrentId(null);
    setAttachments([]);
    setEditingIdx(null);
    setCtxUsed(0);
    setConvTokens(0);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
  }

  function closeTab(id: string) {
    if (streaming) return;
    const idx = tabsRef.current.findIndex((t) => t.id === id);
    if (idx < 0) return;
    // Title generated on close: we analyze the WHOLE conversation we
    // close (not just its first exchange) to set a reliable title
    // in the history. Silent if the conversation has neither messages nor id.
    const closingTab = tabsRef.current[idx];
    if (closingTab && closingTab.messages.length > 0 && closingTab.currentId) {
      void autoTitle(closingTab.currentId, closingTab.messages, id, closingTab.model);
    }
    const next = tabsRef.current.filter((t) => t.id !== id);
    setTabs(next);
    if (activeTabId !== id) return;
    const fallback = next[Math.min(idx, next.length - 1)];
    if (fallback) {
      setMessages(fallback.messages);
      setCurrentId(fallback.currentId);
      if (fallback.model && runningModels.includes(fallback.model)) setModel(fallback.model);
      setSettings(fallback.settings);
      setAttachments(fallback.attachments);
      setCtxUsed(0);
      setConvTokens(0);
      setEditingIdx(null);
      setIoTokens(null);
      updateQueue([]);
      closeArtifact();
      setActiveTabId(fallback.id);
    } else {
      const nid = newTabId();
      setTabs([{ id: nid, title: "", currentId: null, model, settings, attachments: [], messages: [] }]);
      setActiveTabId(nid);
      setMessages([]);
      setCurrentId(null);
      setAttachments([]);
      setEditingIdx(null);
      setCtxUsed(0);
      setConvTokens(0);
      setIoTokens(null);
      updateQueue([]);
      closeArtifact();
    }
  }

  function selectConversation(conv: Conversation) {
    // The history list is BOUNDED: beyond a byte budget, the
    // server only carried the metadata (`messages_omis`). We fetch
    // the content when we really open it — without this detour, the
    // conversation would open on an empty thread, which is worse than the latency
    // we save.
    if (conv.messagesOmis) {
      void fetchConversation(conv.id).then((complete) => {
        if (complete) {
          setConversations((prev) => prev.map((c) => (c.id === complete.id ? complete : c)));
          appliquerConversation(complete);
        } else {
          showToast({ body: t("Cette conversation n'a pas pu être chargée."), type: "error" });
        }
      });
      return;
    }
    appliquerConversation(conv);
  }

  /** Applies a conversation to the displayed thread (open from the history). */
  function appliquerConversation(conv: Conversation) {
    // Same: opening a conversation does not modify it, so it must not
    // move it up nor rewrite the one we leave.
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
    setCurrentId(conv.id);
    setEditingIdx(null);
    // The active tab carries this conversation (title in the tab bar).
    setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, title: conv.title, currentId: conv.id } : t)));
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
    setConvTokens(0);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
  }

  function deleteConversation(id: string) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (csrf) {
      // Deletion is optimistic on screen, but a server refusal is
      // SAID: otherwise the conversation came back on reload, as if the click
      // had done nothing.
      void removeConversation(csrf, id).then((ok) => {
        if (!ok) {
          showToast({
            body: t("Suppression impossible — la conversation est conservée."),
            type: "error",
          });
        }
      });
    }
    if (id === currentId) setCurrentId(null);
  }

  // Star/pin a conversation (browser preference, sorted first).
  function togglePinned(id: string) {
    setPinnedIds((prev) => {
      const next = prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id];
      savePinnedIds(next);
      return next;
    });
  }

  // Snippets: saves the current prompt, inserts, deletes.
  function saveSnippet() {
    const content = input.trim();
    if (!content) return;
    // eslint-disable-next-line react-hooks/purity -- called from a handler
    const snip: Snippet = { id: String(Date.now()), label: content.slice(0, 60), content };
    setSnippets((prev) => { const next = [...prev, snip]; saveSnippets(next); return next; });
  }
  function deleteSnippet(id: string) {
    setSnippets((prev) => { const next = prev.filter((s) => s.id !== id); saveSnippets(next); return next; });
  }
  function insertSnippet(content: string) {
    setInput(content);
    setSnippetsOpen(false);
  }

  // Auto-title: asks the model for a short title, then renames the conversation.
  async function genTitle() {
    if (busyTitle || !messages.length || !currentId) return;
    setBusyTitle(true);
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model, messages: sansImages(messages) });
      const title = res?.title;
      const conv = conversations.find((c) => c.id === currentId);
      if (title && conv) {
        setConversations((prev) => prev.map((c) => (c.id === currentId ? { ...c, title } : c)));
        setTabs((prev) => prev.map((t) => (t.currentId === currentId || t.id === activeTabId ? { ...t, title } : t)));
        void persistConversation(csrf, { ...conv, title });
      }
    } finally {
      setBusyTitle(false);
    }
  }

  // Summary: condenses the conversation, displayed in a copyable dialog.
  async function genSummary() {
    if (!messages.length) return;
    setSummary(t("Génération en cours…"));
    setSummaryOpen(true);
    try {
      const res = await sendJSON<{ summary?: string; error?: string }>("/api/playground/summarize", csrf, { model, messages: sansImages(messages) });
      if (res.summary) setSummary(res.summary);
      else setSummary(res.error ? tServeur(res.error, t) : t("Impossible de générer le résumé."));
    } catch {
      setSummary(t("Impossible de générer le résumé."));
    }
  }

  // Auto-title triggered on the first answer: short summary generated by the
  // model, propagated live to the history + the tab, without a reload.
  async function autoTitle(convId: string, msgs: ChatMsg[], tabId: string, modelForTitle?: string) {
    if (!csrf) {
      showToast({ body: t("Session incomplète — recharge la page."), type: "error" });
      return;
    }
    const titleModel = modelForTitle || model;
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model: titleModel, messages: sansImages(msgs) });
      const title = res?.title;
      if (!title) return;
      setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, title } : c)));
      setTabs((prev) => prev.map((t) => (t.id === tabId || t.currentId === convId ? { ...t, title, currentId: convId } : t)));
      const item: Conversation = {
        id: convId, title,
        // eslint-disable-next-line react-hooks/purity -- handler async
        ts: Date.now(), model: titleModel,
        messages: pourSauvegarde(msgs),
      };
      void persistConversation(csrf, item);
    } catch {
      // silencieux : on garde le titre provisoire (début du prompt)
    }
  }

  // Renaming a generated file: applies the new name (persisted per
  // conversation) and closes the edit. The title updates in the chat card,
  // the panel and the download name.
  function renamedTitle(a: Artifact): string {
    return artifactRenames[artifactRenameKey(currentId, a)] ?? a.title;
  }
  function commitArtifactRename(a: Artifact, title: string) {
    const clean = title.trim();
    if (clean) {
      const key = artifactRenameKey(currentId, a);
      setArtifactRenames((prev) => {
        const next = { ...prev, [key]: clean };
        saveArtifactRenames(next);
        return next;
      });
    }
    setRenamingArtifact(false);
  }

  const [shared, setShared] = useState(false);
  // Snippets / reusable prompts (browser library).
  const [snippetsOpen, setSnippetsOpen] = useState(false);
  const [snippets, setSnippets] = useState<Snippet[]>(loadSnippets);
  // Skills: base skills + those created by the user.
  const [skills, setSkills] = useState<Skill[]>(() => [...BASE_SKILLS, ...loadCustomSkills()]);
  const [skillCreatorOpen, setSkillCreatorOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const customSkills = skills.filter((s) => !s.builtin);

  // Opens the creator (edit if an existing skill is provided) and
  // closes the « / » menu so as not to stack modal + menu.
  function openSkillCreator(skill?: Skill) {
    setEditingSkill(skill ?? null);
    setSkillCreatorOpen(true);
    setInput("");
  }

  function addCustomSkill(s: Skill) {
    const custom = [...customSkills, s];
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  // Edit: we replace the skill with the same id (the others stay).
  function updateCustomSkill(s: Skill) {
    const custom = customSkills.map((c) => (c.id === s.id ? s : c));
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  function deleteCustomSkill(id: string) {
    const custom = customSkills.filter((s) => s.id !== id);
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  // Selecting a skill: we put its (localized) prompt into the input
  // and, if it has one, apply its system prompt to the model (flagging
  // that the provenance becomes « compétence »).
  function selectSkill(s: Skill) {
    setInput(t(s.prompt));
    const sp = s.systemPrompt;
    if (sp) {
      setSettings((prev) => ({ ...prev, system: sp }));
      setSystemProvenance("skill");
    }
  }

  // Auto-title + summary (generated by the model, billed to the budget).
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [busyTitle, setBusyTitle] = useState(false);
  // « contexte » panel: what the model sees (system, files, tokens).
  const [ctxOpen, setCtxOpen] = useState(false);
  // Renaming generated files: persisted map (convId::kind::titre → name).
  const [artifactRenames, setArtifactRenames] = useState<Record<string, string>>(loadArtifactRenames);
  const [renamingArtifact, setRenamingArtifact] = useState(false);
  const [renameArtifactValue, setRenameArtifactValue] = useState("");

  // Export (Markdown/JSON) of the loaded conversation.
  function exportConversation(conv: ExportConversation, fmt: "md" | "json") {
    const name = slugify(convTitleFallback(conv.messages, t("Conversation")));
    if (fmt === "json") downloadText(`${name}.json`, convAsJson(conv), "application/json");
    else downloadText(`${name}.md`, convAsMarkdown(conv, t), "text/markdown");
  }

  // Read-only share link: we create the snapshot then copy the URL.
  // Renaming a conversation (title derived from the 1st message by default).
  function startRename(id: string, title: string) {
    setRenamingId(id);
    setRenameValue(title || t("Conversation"));
  }
  function commitRename(id: string) {
    const title = (renameValue.trim().slice(0, 120) || t("Conversation"));
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
    const conv = conversations.find((c) => c.id === id);
    if (csrf && conv) void persistConversation(csrf, { ...conv, title });
    setRenamingId(null);
  }

  const showToast = useToast();

  async function shareConversation(id: string) {
    if (!csrf) {
      showToast({ body: t("Session incomplète — recharge la page."), type: "error" });
      return;
    }
    try {
      const res = await sendJSON<{ ok: boolean; token: string; error?: string }>("/conversations/share", csrf, { client_id: id });
      if (!res?.ok || !res.token) {
        showToast({ body: t("Partage impossible :") + " " + tServeur(res?.error || "erreur", t), type: "error" });
        return;
      }
      const url = `${window.location.origin}/c/${res.token}`;
      // navigator.clipboard requires a secure context (HTTPS): on the LAN over
      // HTTP it does not exist — we fall back to execCommand, and as a last
      // resort we DISPLAY the link rather than fail silently.
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = url;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (!ok) {
          window.prompt(t("Copiez le lien de partage :"), url);
          setShared(true);
          setTimeout(() => setShared(false), 2500);
          return;
        }
      }
      setShared(true);
      showToast({ body: t("Lien de partage copié (lecture seule, réservé aux connectés).") });
      setTimeout(() => setShared(false), 2500);
    } catch {
      showToast({ body: t("Partage impossible : le serveur n'a pas répondu."), type: "error" });
    }
  }

  /** Reads a TEXT file as an attachment (content inlined into the message).
   *
   *  Three explicit refusals, because all three were silent:
   *  a binary (image, PDF, archive) went to the model as mojibake; a
   *  too large file was announced by a blocking native `alert()`; and a
   *  read failure (`FileReader.onerror`, vanished file, disk) said
   *  nothing at all. The refusal is a message in the UI, not a console.
   */
  function handleFiles(files: FileList | null) {
    if (!files) return;
    let nbImages = attachments.filter((a) => a.image).length;
    for (const file of Array.from(files)) {
      const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      if (IMAGE_EXTENSIONS.includes(ext) || file.type.startsWith("image/")) {
        if (!modelVision[model]) {
          showToast({ body: t("« {name} » : ce modèle ne lit pas les images.").replace("{name}", file.name), type: "error" });
          continue;
        }
        if (nbImages >= IMAGES_PAR_MESSAGE) {
          showToast({ body: t("4 images au plus par message."), type: "error" });
          continue;
        }
        if (file.size > IMAGE_SOURCE_MAX_BYTES) {
          showToast({ body: t("« {name} » dépasse 25 Mo.").replace("{name}", file.name), type: "error" });
          continue;
        }
        nbImages++;
        preparerImage(file)
          .then((url) => setAttachments((prev) => [...prev, { name: file.name, content: "", image: url }]))
          .catch((e: unknown) => showToast({
            body: (e instanceof Error && e.message === "canevas"
              ? t("« {name} » est trop grande (plus de 2 Mo) et ce navigateur bloque sa réduction : réduis-la avant de la joindre.")
              : t("« {name} » n'a pas pu être lu.")).replace("{name}", file.name),
            type: "error",
          }));
        continue;
      }
      if (!ATTACH_EXTENSIONS.includes(ext)) {
        showToast({ body: t("« {name} » : seuls les fichiers texte sont acceptés.").replace("{name}", file.name), type: "error" });
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        showToast({ body: t("« {name} » dépasse 96 Ko — trop gros pour le contexte.").replace("{name}", file.name), type: "error" });
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        setAttachments((prev) => [...prev, { name: file.name, content: String(reader.result) }]);
      };
      reader.onerror = () => {
        showToast({ body: t("« {name} » n'a pas pu être lu.").replace("{name}", file.name), type: "error" });
      };
      reader.readAsText(file);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  // Drag-and-drop on the PAGE: without a handler, the browser navigates to the
  // dropped file and the thread disappears. We accept the drop
  // anywhere in the conversation. The ref avoids re-subscribing on every render.
  const handleFilesRef = useRef<(f: FileList | null) => void>(() => {});
  useEffect(() => { handleFilesRef.current = handleFiles; });
  useEffect(() => {
    function surDepot(e: DragEvent) {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      handleFilesRef.current(e.dataTransfer.files);
    }
    function surSurvol(e: DragEvent) {
      if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
    }
    window.addEventListener("dragover", surSurvol);
    window.addEventListener("drop", surDepot);
    return () => {
      window.removeEventListener("dragover", surSurvol);
      window.removeEventListener("drop", surDepot);
    };
  }, []);

  async function runStream(nextMessages: ChatMsg[]) {
    if (!model) {
      setMessages([...nextMessages, { role: "assistant", content: t("Aucun modèle actif.") }]);
      return;
    }
    setStreaming(true);
    setEtapesWeb([]);
    // New send: the reader wants to see the answer arrive, we rearm the tracking.
    descendreToutEnBas();   // on re-suit le bas quand on envoie
    // « quand un texte est lancé je veux garder la barre latérale, là il la fait
    // disparaitre » : une rédaction de DOCUMENT s'affiche dans le panneau dès le
    // premier mot, comme l'écriture d'un fichier de code le fait déjà — avant,
    // `liveDocOpen` ne devenait vrai qu'au clic sur la carte du chat, et pendant
    // toute la rédaction le panneau restait absent (ou figé sur le fichier
    // précédent). L'état reste celui de la VOLONTÉ de l'utilisateur : « Fermer »
    // pendant la rédaction le remet à faux et rien ne rouvre, la carte
    // « Ouvrir le document en cours de rédaction » le remet à vrai. Sur
    // téléphone il n'y a pas de panneau : le texte défile alors dans le chat.
    const redactionDoc =
      isDocTask(nextMessages[nextMessages.length - 1]?.content ?? "") && !isNarrow;
    setLiveDocOpen(redactionDoc);
    liveDocOpenRef.current = redactionDoc;
    const controller = new AbortController();
    abortRef.current = controller;
    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const startTs = Date.now();
    const withPlaceholder = [...nextMessages, { role: "assistant", content: "", ts: startTs } as ChatMsg];
    setMessages(withPlaceholder);

    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const t0 = performance.now();
    liveCharsRef.current = 0;
    liveStartRef.current = null;
    setLiveStats(null);
    let tf: number | null = null;
    let acc = "";
    let reason = "";
    let raisonDebut: number | null = null;   // 1er fragment de pensée
    let finPensee: number | null = null;     // 1er fragment de réponse
    let usage: { total_tokens?: number; completion_tokens?: number; prompt_tokens?: number } | undefined;

    const updateLast = () => {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", content: acc, reasoning: reason, ts: copy[copy.length - 1]?.ts , reasoningMs: raisonDebut ? Math.round((finPensee ?? performance.now()) - raisonDebut) : undefined };
        return copy;
      });
    };

    let isError = false;
    let tronque = false;
    let wasAborted = false;
    try {
      // We remove the "asking questions" capability ONLY on the turn that
      // immediately follows answers — where the model would be tempted to chain
      // question upon question instead of answering.
      // Before, this test swept the WHOLE conversation (`some`): as soon as we had
      // answered once, the model could never ask for details again,
      // even much later on an unrelated request. Hence the impression
      // that it "charged ahead" while it was still asking questions at the start of
      // the conversation (measured: 16 vague requests out of 18 lead to a
      // question, and 0 out of 9 precise requests).
      const dernier = nextMessages[nextMessages.length - 1];
    // Is this turn a resume (« Continuer » button or automatic resume)?
    const enReprise = estReprise(dernier);
      const alreadyAsked = !!dernier?.hidden;
      const plafondModele = modelLimits[model];
      const askSettings = {
        ...settings,
        // The output ceiling cannot exceed the loaded model's window.
        // The backend already lowers it to what remains; here we avoid sending a
        // value that makes sense for no current model.
        maxTokens: plafondModele ? Math.min(settings.maxTokens, plafondModele) : settings.maxTokens,
        // Order and VOLUME matter: measured on 12 vague requests, the model
        // asks a question 11 times out of 12 with the questions instruction alone,
        // 7 times out of 12 when adding naming and editing, and 3 times out of 12 if
        // the questions instruction goes last. So we keep the questions
        // AT THE TOP, a naming reduced to one sentence, and the edit instruction
        // only when a file already exists — before that, it is useless.
        // On a RESUME turn, neither questions nor edit protocol: the model
        // had precisely answered with a truncated ```edit block instead of finishing
        // the file, leaving the user with nothing.
        system: (enReprise
          ? [settings.system.trim(), REPRISE_INSTRUCTION]
          : [
              settings.system.trim(),
              alreadyAsked ? "" : ASK_INSTRUCTION,
              NAME_INSTRUCTION,
              fichiersJusqua(nextMessages, nextMessages.length - 1, t).size ? REWRITE_INSTRUCTION : "",
              INTEGRALITE_INSTRUCTION,
            ]
        ).filter(Boolean).join("\n\n"),
      };
      await streamChat(
        csrf,
        model,
        imagesRecentes(nextMessages, IMAGES_MAX_REQUETE).map((m) => ({ role: m.role, content: m.content, images: m.images })),
        askSettings,
        controller.signal,
        (delta) => {
          if (delta.usage) usage = delta.usage;
          if (delta.truncated) tronque = true;
          if (delta.notice) {
            // System notice (quota exceeded…): translated HERE according to the
            // interface language, not server-side.
            acc += (acc ? "\n\n" : "") + texteNotice(delta.notice, t);
            updateLast();
          }
          if (delta.webStep) {
            const e = delta.webStep;
            // A "finished" step replaces its announcement, it is not added.
            // The portail can send an EMPTY frame ({}) when a tool refused
            // before doing anything: no step, therefore nothing to replace —
            // without this guard `etape.endsWith` threw and the answer was
            // lost as a network error.
            setEtapesWeb((prec) => {
              const base = (e.etape ?? "").endsWith("_finie") ? prec.slice(0, -1) : prec;
              return [...base, e].slice(-6);
            });
          }
          if (delta.reasoningChunk) {
            if (tf === null) tf = performance.now();
            if (raisonDebut === null) raisonDebut = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.reasoningChunk.length;
            reason += delta.reasoningChunk;
            updateLast();
          }
          if (delta.contentChunk) {
            if (tf === null) tf = performance.now();
            if (finPensee === null) finPensee = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.contentChunk.length;
            acc += delta.contentChunk;
            updateLast();
          }
        },
      );
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        wasAborted = true; // Deliberate stop (Stop button): not an error.
      } else {
        isError = true;
        if (!acc) acc = t("Erreur réseau.");
      }
    }
    if (!isError && !wasAborted && !acc && !reason) {
      isError = true;
      acc = t("Le modèle n'a renvoyé aucune réponse.");
    }

    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const te = performance.now();
    const finalMessages = [...nextMessages];
    if (acc || reason) {
      const gen = tf ? (te - tf) / 1000 : 0;
      const tokens = usage?.completion_tokens;
      // Auto-calibration: the true token count is known here, we derive the
      // real characters/token ratio of this model/language so the estimate
      // of the NEXT generation is right. Bounded so that a degenerate
      // answer (1 token, 500 characters) does not skew the display for good.
      const producedChars = acc.length + reason.length;
      if (tokens && producedChars > 0) {
        charsPerTokenRef.current = Math.min(12, Math.max(1.5, producedChars / tokens));
      }
      finalMessages.push({
        role: "assistant",
        content: trimAfterAsk(acc),
        truncated: tronque,
        reasoning: reason,
        reasoningMs: raisonDebut ? Math.round((finPensee ?? te) - raisonDebut) : undefined,
        tokens,
        tokensPerSec: tokens && gen > 0 ? Number((tokens / gen).toFixed(1)) : undefined,
        ttft: tf ? Number(((tf - t0) / 1000).toFixed(2)) : undefined,
        ts: startTs,
        isError,
      });
    }
    // Did the THREAD change during the generation? Loading another conversation
    // from the history (or switching tab) is not blocked — it is
    // legitimate — but the end of the stream then rewrote `messages` AND `currentId`
    // with the values captured at START: we brutally jumped back to the old
    // conversation, the partial answer added on top, and the active tab
    // kept the other one's title. We always save the answer in the right
    // place (no loss), we only repaint the screen if we are still there.
    const memeFil = currentIdRef.current === currentId;
    if (memeFil) setMessages(finalMessages);
    // If the assistant wrote a file, or wrote a document in reply to a document
    // task, surface the last one in the side panel automatically.
    const lastUser = [...nextMessages].reverse().find((mm) => mm.role === "user");
    // A clarifying question is never a document/file artifact — leave the panel closed.
    const produced = parseAsk(acc)
      ? []
      : parseArtifacts(contenuCloture(acc), isDocTask(lastUser?.content ?? ""), t).artifacts;
    if (produced.length && memeFil) {
      const lastArt = produced[produced.length - 1];
      // A code file opens on its own; a document opens automatically only if the
      // user was already watching it being written live (otherwise it stays a
      // card in the chat that they can click open).
      if (lastArt.kind === "code" || liveDocOpenRef.current) setArtifact(lastArt);
    }
    liveDocOpenRef.current = false;
    const total = usage?.total_tokens;
    if (total && memeFil) {
      setCtxUsed(total);
      setConvTokens((p) => p + total);
    }
    if (memeFil && (typeof usage?.prompt_tokens === "number" || typeof usage?.completion_tokens === "number")) {
      setIoTokens({ prompt: usage?.prompt_tokens ?? 0, completion: usage?.completion_tokens ?? 0 });
    }
    const savedId = persist(finalMessages, currentId, model);
    if (memeFil) {
      setCurrentId(savedId ?? null);
      // The active tab immediately carries this conversation (even before
      // the auto-title answers), so that closing/switching stays consistent.
      setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, currentId: savedId ?? null } : t)));
    }
    // (Auto-title on close only — we analyze the full conversation
    // for a reliable history title; see closeTab.)
    setStreaming(false);
    setLiveStats(null);
    abortRef.current = null;
    // The model dropped mid-file: we chain on our own. Seen in
    // production — a large HTML file stopped mid-expression without
    // anything indicating it, and everything had to be asked again. The token
    // ceiling had nothing to do with it: the model hands back too early.
    // `isError` does NOT rule out a resume: the most frequent cut is precisely
    // an error — the stream breaks along the way on a generation of several
    // minutes, and the text already received stops mid-word. This is exactly the
    // case where resuming on our own is most valuable. Only a REQUESTED stop (Stop
    // button) forbids the resume.
    // Aborted turn (announcement without the follow-up): we REDO the turn instead of
    // resuming it — there is nothing to extend, the answer never started.
    if (!wasAborted && tourAvorte(finalMessages[finalMessages.length - 1])) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        void runStream(nextMessages);
        return;
      }
    }
    // `messageIncomplet` below already evaluates `fichierLaisseOuvert`: we keep
    // its result instead of recomputing it two lines below (each call
    // re-reads the whole message, resumes included).
    const dernierIdx = finalMessages.length - 1;
    const laisseOuvert = !wasAborted
      && fichierLaisseOuvert(finalMessages, dernierIdx, t);
    if (!wasAborted
        && (laisseOuvert || abandonDeclare(finalMessages[dernierIdx]?.content ?? ""))) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        // File left OPEN: we extend it to the next character. File
        // CLOSED but abbreviated by the model's own admission: extending it would
        // produce content after the last line of an already closed file — what is
        // needed is a full rewrite request.
        const suite = laisseOuvert ? PROMPT_REPRISE_COMPLET : PROMPT_INTEGRAL;
        void runStream([...finalMessages, {
          role: "user", content: suite,
          // eslint-disable-next-line react-hooks/purity -- called from a handler
          ts: Date.now(), hidden: true,
        }]);
        return;
      }
      // Trop de reprises d'affilée : le bandeau et le bouton prennent le relais.
    }
    reprisesRef.current = 0;
    setReprise(0);
    // Messages were queued during this generation: they go out
    // now, with no manual validation. Explicit base (finalMessages):
    // messagesRef is not yet resynchronized in this same tick.
    if (queuedRef.current.length) dispatchQueued(finalMessages);
  }

  function send(value: string) {
    const text = value.trim();
    if (!text && !attachments.length) return;
    // The key may meanwhile have been created from the settings box: we
    // re-check here rather than leave the banner (and the failure) lingering.
    // One extra request, and only in the broken state.
    if (hasKey === false) {
      void fetchPlaygroundData().then((d) => setHasKey(d.has_key)).catch(() => {});
    }
    // Sending ends dictation: the message goes out with what has been
    // transcribed so far, and no still-in-flight pass will rewrite the
    // field once it's been cleared.
    dictation.cancel();
    const fichiers = attachments.filter((f) => !f.image);
    let images: string[] | undefined = attachments.flatMap((f) => (f.image ? [f.image] : []));
    // The model may have changed since the attachment: we do not send images
    // it would not read (the server would drop them anyway).
    if (images.length && !modelVision[model]) {
      showToast({ body: t("Ce modèle ne lit pas les images : elles n'ont pas été envoyées."), type: "error" });
      images = [];
    }
    if (!images.length) images = undefined;
    if (!text && !fichiers.length && !images) return;
    let full = text;
    if (fichiers.length) {
      full =
        (text ? text + "\n\n" : "") +
        fichiers.map((f) => "```" + f.name + "\n" + f.content + "\n```").join("\n\n");
    }
    const attachmentCount = fichiers.length || undefined;
    // Mid-generation, the message goes to the queue (validated by the
    // « Envoyer » button of its bubble) instead of being silently lost.
    if (streaming) {
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      updateQueue([...queuedRef.current, { content: full, text, attachmentCount, images, ts: Date.now() }]);
      setInput("");
      setAttachments([]);
      return;
    }
    const nextMessages: ChatMsg[] = [
      // In inline edit: we rebranch from the edited message — we keep
      // everything that precedes it, we replace this message + the rest with the
      // new version.
      ...(editingIdx !== null ? messages.slice(0, editingIdx) : messages),
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      { role: "user", content: full, ts: Date.now(), attachmentCount, images },
    ];
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
    setEditingIdx(null);
    void runStream(nextMessages);
  }

  // « Envoyer » on a row: do not wait for the current answer to end.
  // We move this message to the head of the queue then interrupt the generation —
  // its start is kept, exactly as with Stop. The send itself is done by
  // the end of runStream (the abort is asynchronous: reading state here would
  // lose the partial answer).
  function sendQueuedNow(idx: number) {
    const q = queuedRef.current;
    if (!q[idx]) return;
    updateQueue([q[idx], ...q.filter((_, i) => i !== idx)]);
    if (streaming) {
      abortRef.current?.abort();
      return;
    }
    dispatchQueued();
  }

  // « Modifier »: the message comes out of the queue and goes back into the composer.
  // We put back `content` (not the raw text) so as not to silently lose the
  // attached files that were inlined into it.
  function editQueued(idx: number) {
    const m = queuedRef.current[idx];
    if (!m) return;
    updateQueue(queuedRef.current.filter((_, i) => i !== idx));
    setInput(m.content);
  }

  function dispatchQueued(base?: ChatMsg[]) {
    const q = queuedRef.current;
    if (!q.length) return;
    updateQueue([]);
    const msgs: ChatMsg[] = q.map((m) => ({ role: "user", content: m.content, ts: m.ts, attachmentCount: m.attachmentCount, images: m.images }));
    const nextMessages = [...(base ?? messagesRef.current), ...msgs];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  function discardQueued(idx: number) {
    updateQueue(queuedRef.current.filter((_, i) => i !== idx));
  }

  // Answer a clarifying question the model asked (clicking an option, or the
  // free-text "Other"): send the chosen answer as a user message and continue.
  function answer(text: string) {
    if (streaming) return;
    const t2 = text.trim();
    if (!t2) return;
    // `hidden`: the answers go to the model but are not shown in the chat — the
    // user's choices already live in the (now locked) question card.
    // eslint-disable-next-line react-hooks/purity -- answer() only runs from a handler
    const nextMessages: ChatMsg[] = [...messages, { role: "user", content: t2, ts: Date.now(), hidden: true }];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Asks again for the WHOLE file when the model only returned a snippet. */
  function demanderFichierComplet(nom: string) {
    if (streaming) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Renvoie le fichier ${nom} EN ENTIER, du début à la fin, `
          + "avec la correction intégrée. Un seul bloc de code, aucun extrait, "
          + "aucune ligne omise, pas de « ... » ni de commentaire du type "
          + "« reste inchangé ».",
        // eslint-disable-next-line react-hooks/purity -- called from a handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Resumes an answer cut by the token ceiling, without redoing it. */
  function continuer() {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: PROMPT_REPRISE_COMPLET,
        // eslint-disable-next-line react-hooks/purity -- called from a handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** The delivered file does not run: we ask again for a complete version.
   *
   * Definitely NOT a resume: the cut is not at the end, it is in the middle
   * (a never-closed block). Adding text after it would change nothing.
   */
  function refaireFichier(nom: string) {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Le fichier \`${nom}\` ne fonctionne pas : son JavaScript ne compile pas `
          + "(« Unexpected end of input » — un bloc n'est jamais refermé), donc la page reste "
          + "vide. Renvoie le fichier COMPLET et corrigé, en entier, du début à la fin, "
          + "sous le même nom. Vérifie que chaque accolade et chaque parenthèse est refermée.",
        // eslint-disable-next-line react-hooks/purity -- called from a handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** The preview raised an error: we ask again for the fixed file, in full. */
  function corrigerErreur(nom: string, message: string) {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Le fichier \`${nom}\` s'ouvre mais ne fonctionne pas. Le navigateur `
          + `signale : « ${message} ». Corrige la cause exacte de cette erreur `
          + "(vérifie notamment que les fonctions appelées existent bien dans la version "
          + "de bibliothèque que tu utilises) et renvoie le fichier COMPLET corrigé, "
          + "en entier, sous le même nom.",
        // eslint-disable-next-line react-hooks/purity -- called from a handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  function stop() {
    abortRef.current?.abort();
  }

  function regenerate() {
    if (streaming || !messages.length) return;
    const last = messages[messages.length - 1];
    const base = last.role === "assistant" ? messages.slice(0, -1) : messages;
    if (base.length && base[base.length - 1].role === "user") void runStream(base);
  }

  // Editing a PAST message, in place: the conversation stays displayed, the
  // content goes back into the input, and sending will rebranch from that point
  // (the edited message + the rest are replaced, not truncated on display).
  function editMessage(i: number) {
    if (streaming) return;
    const m = messages[i];
    if (!m || m.role !== "user") return;
    setEditingIdx(i);
    setInput(m.content);
    setAttachments((m.images ?? []).map((u, k) => ({ name: `image-${k + 1}.jpg`, content: "", image: u })));
  }


  const max = modelLimits[model] || 32768;
  // A single character sum and a single estimate per render:
  // `estimateTokens` was called TWICE with the same arguments, and the
  // breakdown below redid the same sum a third time.
  let contentChars = input.length;
  for (const m of messages) contentChars += m.content.length;
  for (const a of attachments) contentChars += a.image ? IMAGE_POIDS_CHARS : a.content.length;
  for (const m of messages) contentChars += (m.images?.length ?? 0) * IMAGE_POIDS_CHARS;
  const estTokens = Math.round((settings.system.length + contentChars) / 4);
  // Input / output: the window fills with INPUT tokens (prompt: system
  // + history + message + files) and OUTPUT tokens (the generated
  // answer). Exact measurement of the last generation (LiteLLM usage) when it
  // exists; otherwise a chars/4 estimate of the current input.
  const used = Math.max(ctxUsed, estTokens);
  const inTokens = ioTokens?.prompt ?? estTokens;
  const outTokens = ioTokens?.completion ?? 0;
  // Content breakdown for « Fenêtre de contexte »: system prompt vs
  // messages/files (same chars/4 estimate). The backend does not provide the
  // per-segment count, so we estimate the relative share — faithful enough to
  // visualize what occupies the window.
  const systemTokens = Math.round(settings.system.length / 4);
  const contentTokens = Math.round(contentChars / 4);
  const totalEstimate = Math.max(1, systemTokens + contentTokens);
  const sysPct = Math.round((systemTokens / totalEstimate) * 100);
  const msgPct = 100 - sysPct;
  const ctxLevel: "accent" | "warning" | "error" =
    used / max >= 0.95 ? "error" : used / max >= 0.8 ? "warning" : "accent";

  // History list: search by title + pinned first, then recent.
  const q = histQuery.trim().toLowerCase();
  // `filter().sort()` over all conversations was redone on EVERY render,
  // hence on every token received, while the list is only read in the
  // « Historique » box: we only compute it when it is open.
  const visibleConvs = useMemo(
    () => (historyOpen
      ? conversations
          .filter((c) => !q || (c.title || "").toLowerCase().includes(q))
          .sort((a, b) => {
            const pa = pinnedIds.includes(a.id) ? 0 : 1;
            const pb = pinnedIds.includes(b.id) ? 0 : 1;
            return pa - pb || ((b.ts ?? 0) - (a.ts ?? 0));
          })
      : []),
    [historyOpen, conversations, q, pinnedIds],
  );
  const lastMsg = messages[messages.length - 1];
  // While a document is being streamed, the chat shows a card (not the raw text)
  // and the side panel can show a live view of the document being written.
  const streamingDocActive =
    streaming && lastMsg?.role === "assistant" && isDocTask(messages[messages.length - 2]?.content ?? "");
  const liveContent = streamingDocActive ? (lastMsg?.content ?? "") : "";
  // Code file being written: it displays DIRECTLY in the panel,
  // not in the chat. On a narrow screen there is no panel — we then let it
  // scroll in the chat, otherwise the user would see nothing being written.
  const liveCode =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? openCodeFence(lastMsg.content ?? "")
      : null;
  const liveCodeTitle = liveCode
    ? titleFromContext((lastMsg?.content ?? "").slice(0, liveCode.start)) || `${liveCode.lang}`
    : "";
  const showLiveDoc = liveDocOpen && streamingDocActive;
  // Between two files (previous block closed, next not started), the panel
  // keeps the last finished file instead of emptying. Derived from content, no
  // state: nothing to synchronize, hence nothing to desynchronize.
  const dernierFini =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? (parseArtifacts(lastMsg.content ?? "", false, t).artifacts.slice(-1)[0] ?? null)
      : null;
  const epingle = liveCode ? null : (dernierFini ?? artifact);
  const showLive = showLiveDoc || !!liveCode;
  // Unified panel values: file being written, last finished file, live document,
  // else the pinned artifact.
  const panelIsCode = !!liveCode || epingle?.kind === "code";
  const panelTitle = liveCode
    ? liveCodeTitle
    : showLiveDoc
      ? docTitleFromContent(liveContent)
      : (epingle ? renamedTitle(epingle) : "Document");
  const canRenamePanel = !!epingle && !liveCode && !showLiveDoc;
  const panelContent = liveCode
    ? liveCode.body
    : showLiveDoc
      ? liveContent
      : (epingle?.content ?? "");
  const panelSubtitle = liveCode
    ? t("Écriture en cours…")
    : showLiveDoc
      ? t("Rédaction en cours…")
      : (panelIsCode && epingle?.kind === "code" ? epingle.lang : "");
  const panelLang = liveCode ? liveCode.lang : (epingle?.kind === "code" ? epingle.lang : "text");
  // A finished HTML page can be WATCHED, not only read as code. We do not
  // offer it while it is being written: a half-written render flickers.
  const panelEstHtml = !liveCode && panelIsCode && /^html?$/i.test(panelLang);
  const panelDownloadName = panelIsCode
    ? nomTelechargeable(panelTitle, panelLang)
    : `${slugify(panelTitle)}.md`;
  const panelDownloadMime = panelIsCode ? mimePourLangage(panelLang) : "text/markdown";
  // Publishes the page to preview as soon as its content changes. External write
  // (network) followed by a state update AFTER the await: no cascading
  // render. Nothing is published while no preview is being watched.
  useEffect(() => {
    let annule = false;
    if (!panelEstHtml || !htmlPreview || !panelContent) {
      // Deferred reset: updating state in the BODY of the effect
      // would trigger a cascading render.
      void Promise.resolve().then(() => { if (!annule) setPreviewUrl(""); });
      return () => { annule = true; };
    }
    void sendJSON<{ ok: boolean; id?: string }>("/playground/preview", csrf, { html: panelContent })
      .then((r) => {
        if (annule || !r.ok || !r.id) return;
        setErreurApercu("");            // new page: we start from a clean state
        setPreviewUrl(`/playground/preview/${r.id}`);
      })
      .catch(() => {});
    return () => { annule = true; };
  }, [panelEstHtml, htmlPreview, panelContent, csrf]);

  // The preview reports its runtime errors. A page can be perfectly
  // well-formed and do nothing — wrong library version, non-existent
  // method — and no text analysis can see it. Only execution
  // says so, and the preview is what executes.
  // Legitimate preview windows. The iframe has an OPAQUE origin (sandbox without
  // allow-same-origin), so `e.origin` is « null » and proves nothing: without
  // checking `e.source`, any window holding a reference to
  // this page (frame, `window.opener`) could inject a fake error
  // message — displayed in a Banner, then sent back to the model via « Corriger ».
  // A WeakSet covers BOTH preview iframes (they are not necessarily
  // mounted exclusively depending on the layout) and retains nothing.
  const apercuWindows = useRef(new WeakSet<Window>());
  const refApercu = useCallback((el: HTMLIFrameElement | null) => {
    const w = el?.contentWindow;
    if (w) apercuWindows.current.add(w);
  }, []);

  useEffect(() => {
    function surMessage(e: MessageEvent) {
      if (!e.source || !apercuWindows.current.has(e.source as Window)) return;
      const d = e.data as { cronosPreviewError?: unknown } | null;
      const msg = d && typeof d.cronosPreviewError === "string" ? d.cronosPreviewError : null;
      if (!msg) return;
      // Content produced by the generated page: never interpreted, only displayed.
      setErreurApercu((prec) => prec || msg.slice(0, 300));
    }
    window.addEventListener("message", surMessage);
    return () => window.removeEventListener("message", surMessage);
  }, []);
  // Auto-follow the document while it streams into the panel; show a "jump to
  // bottom" button when the reader scrolls up and leaves the live tail.
  const {
    setRef: panelScrollRef,
    showButton: showPanelJump,
    onScroll: onPanelScroll,
    scrollToBottom: panelJumpDown,
  } = useStickToBottom(panelContent, showLive);
  // Time-of-day greeting for the first message (Claude-style). « soir » from 18h.
  const playHour = new Date().getHours();
  const greeting =
    playHour >= 18 || playHour < 5
      ? t("Bonsoir, comment allez-vous ?")
      : t("Bonjour, comment allez-vous ?");
  // First message: we center the composer with the greeting above.
  const isFirstEmpty = messages.length === 0 && !isSettingsOpen && !streaming;

  // Rotating placeholder: a few texts that rotate (including the « / » trick
  // to call a skill). The timer is independent of rendering: it simply
  // advances the index in PLACEHOLDER_TEXTS.
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setPlaceholderIdx((i) => (i + 1) % PLACEHOLDER_TEXTS.length), 5000);
    return () => clearInterval(id);
  }, []);
  const placeholderText = t(PLACEHOLDER_TEXTS[placeholderIdx] ?? PLACEHOLDER_TEXTS[0]);

  // « /compétence » mode: as soon as the input starts with a « / », we show
  // the skills menu. The text after « / » acts as a filter.
  const slashQuery = input.startsWith("/") ? input.slice(1).trim().toLowerCase() : null;

  // Special « / » commands: /skill-creator (or /create, /new) opens the
  // skill creator and clears the input. Detected on typing (onChange),
  // not in an effect, to respect the react-hooks/set-state-in-effect rule.
  function handleInput(v: string) {
    const cmd = v.startsWith("/") ? v.slice(1).trim().toLowerCase() : null;
    if (cmd && SLASH_CREATE_COMMANDS.includes(cmd)) {
      openSkillCreator();
      return;
    }
    setInput(v);
  }

  // Filtered skills (for keyboard navigation + the menu): base then created.
  const baseHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  const customHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => !s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  // Row 0 = « créer » card, rows 1..n = skills (base then created).
  const skillHits = useMemo(() => [...baseHits, ...customHits], [baseHits, customHits]);
  const menuRows = 1 + skillHits.length;

  // Highlighted row in the menu (0 = « Créer un skill », 1..n = skills).
  const [skillSel, setSkillSel] = useState(1);
  useEffect(() => {
    // We move the highlight back to the first skill on every filter
    // change (sync from the input state, legitimate case allowed by the lint).
    if (slashQuery !== null) {
      /* eslint-disable react-hooks/set-state-in-effect */
      setSkillSel(1);
      /* eslint-enable react-hooks/set-state-in-effect */
    }
  }, [slashQuery]);

  // Keyboard navigation handler, kept up to date on every render via a ref:
  // the listener effect only depends on the menu being open, with no unstable
  // dependencies (recreated functions / arrays), hence 0 exhaustive-deps warnings.
  const onMenuKeyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    onMenuKeyRef.current = (e: KeyboardEvent) => {
      // No skill matches the filter: this is not a command,
      // it is a message that STARTS with « / » (a file path, a
      // command from another tool…). The menu is then just a « créer un
      // skill » row, and Enter opened the creator WHILE CLEARING THE INPUT: the
      // written message was destroyed without a word. We no longer touch the
      // keyboard in this case — Enter sends, as for any message.
      if (!skillHits.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.min(s + 1, menuRows - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.max(s - 1, 0));
      } else if (e.key === "Escape") {
        // Escape NEVER clears the input: it used to "close" the menu, but
        // the menu has no state of its own (it follows the input text), so the
        // key erased everything that was typed. We leave it to the composer.
        e.stopPropagation();
      } else if (e.key === "Enter") {
        const sel = Math.min(skillSel, menuRows - 1);
        if (sel === 0) {
          // The « créer un skill » row is only reachable by keyboard with ↑/↓
          // when skills exist: we no longer open it by accident.
          e.preventDefault();
          e.stopPropagation();
          openSkillCreator();
        } else {
          const s = skillHits[sel - 1];
          if (!s) return;
          e.preventDefault();
          e.stopPropagation();
          selectSkill(s);
        }
      }
    };
  });

  useEffect(() => {
    if (slashQuery === null) return;
    function onMenuKey(e: KeyboardEvent) {
      onMenuKeyRef.current(e);
    }
    // CAPTURE-PHASE listener on the window to beat the input (otherwise
    // ↑/↓ re-reads the history and Enter sends the message).
    window.addEventListener("keydown", onMenuKey, true);
    return () => window.removeEventListener("keydown", onMenuKey, true);
  }, [slashQuery]);
  const effectiveSel = Math.min(skillSel, menuRows - 1);

  // « composer » node reused both in the bottom-anchored layout (ongoing
  // conversation) and centered on the first message. Bottom bar: Attacher (left),
  // model selector + mic/send button (right).
  const composerNode = (
    <VStack gap={2} padding={4}>
      {/* Édition en place : la conversation reste affichée, on signale que le
          prochain envoi rebranchera depuis le message édité, avec annulation. */}
      {editingIdx !== null && (
        <Banner
          status="info"
          title={t("Message en cours de modification")}
          description={t("Le prochain envoi remplacera ce message et la suite de la conversation.")}
          endContent={
            <Button
              label={t("Annuler")}
              variant="ghost"
              size="sm"
              onClick={() => { setEditingIdx(null); setInput(""); }}
            />
          }
        />
      )}
      {/* Sans clé API, le playground ne peut rien envoyer : il tourne sur
          la clé de l'utilisateur. On le dit AVANT la première question,
          avec le bouton qui mène pile au bon endroit — plutôt que de
          laisser découvrir le problème par un message d'erreur. */}
      {hasKey === false && (
        <Banner
          status="warning"
          title={t("Aucune clé API")}
          description={t(
            "Le playground consomme le budget de ton compte via ta clé API. Crée-en une pour pouvoir discuter avec le modèle.",
          )}
          endContent={
            <Button
              label={t("Créer une clé API")}
              variant="primary"
              size="sm"
              icon={<Icon icon={KeyIcon} size="sm" />}
              onClick={() => openSettings("keys")}
            />
          }
        />
      )}
      {/* File d'attente, juste au-dessus du compositeur : les messages
          tapés pendant une génération attendent ici et partent seuls dès
          qu'elle se termine. Les actions ne servent qu'à ne pas attendre
          (Envoyer), reprendre le texte (Modifier) ou annuler (croix). */}
      {queued.length > 0 && (
        <Card
          variant="muted"
          padding={3}
          style={{ border: "var(--border-width) solid var(--color-border-emphasized)" }}>
          <VStack gap={2}>
            <HStack hAlign="between" vAlign="center" gap={2}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{t("Messages en attente")}</Text>
                <Badge label={String(queued.length)} variant="warning" />
              </HStack>
              <Icon icon={ClockIcon} size="sm" color="secondary" />
            </HStack>
            {queued.map((q, i) => (
              <HStack key={`queued-${q.ts}-${i}`} gap={2} vAlign="center">
                <StackItem size="fill">
                  <Text maxLines={1} color="secondary">{q.text || q.content}</Text>
                </StackItem>
                <Button
                  label={t("Modifier")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={PencilIcon} size="sm" />}
                  onClick={() => editQueued(i)}
                />
                <Button
                  label={t("Envoyer")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                  onClick={() => sendQueuedNow(i)}
                />
                <Button
                  label={t("Retirer")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={XMarkIcon} size="sm" />}
                  onClick={() => discardQueued(i)}
                />
              </HStack>
            ))}
            <Text type="supporting" color="secondary">
              {streaming
                ? t("Envoi automatique dès la fin de la réponse. « Envoyer » interrompt et passe à ce message.")
                : t("Envoi imminent…")}
            </Text>
          </VStack>
        </Card>
      )}
      {/* Deux effets, deux signaux, UN SEUL hôte : la carte du composeur.
          Le faisceau tourne pendant que le modèle travaille, le halo monte avec
          la voix pendant la dictée — au repos, le composeur reste sobre, c'est
          ce qui en fait des signaux et pas des ornements.

          Les deux enveloppent la carte, et non le champ de saisie : c'est le
          « chat input » de la librairie (sa lumière naît du bord bas de son
          hôte) et le seul élément qui ait une surface et un rayon. Le champ,
          lui, est un ruban transparent de 30 px sans rayon : la lumière y
          flottait au-dessus du texte, avec les coins de 16 px du repli de la
          librairie dans une carte qui en fait 28.

          `borderRadius` est fourni à la main (cf. `rayonComposeur`) : la
          détection automatique des deux librairies lit leur PREMIER ENFANT, et
          celui de `border-beam` est sa propre balise `<style>` — 0 px, donc le
          repli de 16 px, coins visiblement plus carrés que la carte. Le rognage
          des deux enveloppes, lui, est réglé dans globals.css (`overflow: clip`
          élargi de 8 px) : c'est lui, et non le `clip-path` des calques, qui
          contient les lumières. */}
      <BorderBeam active={streaming} theme={mode === "system" ? "auto" : mode}
                  borderRadius={rayonComposeur}
                  className="composeur-cadre">
      <VoiceBeam stream={dictation.stream}
                 active={dictation.isRecording || dictation.isTranscribing}
                 processing={dictation.isTranscribing}
                 theme={mode === "system" ? "auto" : mode}
                 borderRadius={rayonComposeur}
                 className="composeur-halo">
      <ChatComposer
        value={input}
        onChange={handleInput}
        onSubmit={send}
        isStopShown={streaming}
        onStop={stop}
        placeholder={placeholderText}
        input={
          <ChatComposerInput value={input} onChange={handleInput} onSubmit={send}
                                  // Pasting or dropping a file in the input:
                                  // the component calls `onFiles` — the page did not
                                  // pass it, so the event was swallowed without
                                  // saying anything (unconditional `preventDefault()`).
                                  onFiles={(files) => handleFiles(files as unknown as FileList)} />
        }
        drawer={
          attachments.length ? (
            <ChatComposerDrawer count={attachments.length} label={t("Fichiers joints")}>
              <VStack gap={2}>
                {attachments.some((f) => f.image) && (
                  <HStack gap={1} wrap="wrap">
                    {attachments.map((f, i) => f.image ? (
                      <Thumbnail
                        key={f.name + i}
                        src={f.image}
                        alt={f.name}
                        label={f.name}
                        onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                      />
                    ) : null)}
                  </HStack>
                )}
                {attachments.some((f) => !f.image) && (
                  <HStack gap={1} wrap="wrap">
                    {attachments.map((f, i) => f.image ? null : (
                      <Token
                        key={f.name + i}
                        label={`${f.name} (${Math.ceil(f.content.length / 1024)} Ko)`}
                        onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                      />
                    ))}
                  </HStack>
                )}
              </VStack>
            </ChatComposerDrawer>
          ) : undefined
        }
        footerActions={
          <Button
            label={modelVision[model] ? t("Joindre un fichier ou une image") : t("Joindre un fichier")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={PaperClipIcon} size="sm" />}
            onClick={() => fileInputRef.current?.click()}
          />
        }
        sendActions={
          <Selector
            label={t("Modèle")}
            isLabelHidden
            size="sm"
            placeholder={t("Aucun modèle actif")}
            options={runningModels}
            value={model}
            onChange={(v) => setModel(v ?? "")}
          />
        }
        sendButton={
          streaming ? (
            <Button
              label={t("Arrêter")}
              variant="primary"
              isIconOnly
              size="md"
              icon={<Icon icon={StopIcon} size="sm" />}
              onClick={stop}
            />
          ) : input.trim().length > 0 || attachments.length > 0 ? (
            <Button
              label={t("Envoyer")}
              variant="primary"
              isIconOnly
              size="md"
              icon={<Icon icon={ArrowUpIcon} size="sm" />}
              onClick={() => send(input)}
            />
          ) : (
            <DictateButton dictation={dictation} isDisabled={false} size="md" />
          )
        }
      />
      </VoiceBeam>
      </BorderBeam>
      {/* Menu des compétences : affiché dès qu'on tape « / » (pour en
          sélectionner une) juste sous le champ. */}
      {slashQuery !== null && (
        <SkillsMenu
          baseSkills={baseHits}
          customSkills={customHits}
          query={slashQuery}
          selectedIndex={effectiveSel}
          onSelect={selectSkill}
          onCreate={() => openSkillCreator()}
          onEdit={(s) => openSkillCreator(s)}
          onDelete={deleteCustomSkill}
        />
      )}
      {imageVue && (
        <Lightbox
          isOpen
          onOpenChange={(o) => { if (!o) setImageVue(null); }}
          media={imageVue.srcs.map((src, k) => ({ src, alt: t("Image jointe {n}").replace("{n}", String(k + 1)) }))}
          index={imageVue.index}
          onIndexChange={(index) => setImageVue((v) => (v ? { ...v, index } : v))}
          hasZoom
        />
      )}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={modelVision[model] ? `${ATTACH_ACCEPT},${IMAGE_ACCEPT}` : ATTACH_ACCEPT}
        style={{ display: "none" }}
        onChange={(e) => handleFiles(e.target.files)}
      />
      {/* L'indice de bas de composeur est réservé aux écrans LARGES. Mesuré au
          gabarit 390 px : les boutons en occupent 236 des 326 px, il ne reste
          que ~82 px au texte, qui s'y étale sur 5 lignes — la rangée monte à
          100 px de haut, plus que la carte de saisie elle-même (94 px). Sur
          bureau le même texte tient sur une ligne (338 px, rangée de 28 px).
          Rien n'est perdu sur téléphone : le sélecteur de fichiers filtre déjà
          par `accept`, et le budget du compte est affiché sur l'accueil.
          `hAlign` suit, sinon la disparition du premier enfant ramènerait les
          boutons à gauche. */}
      <HStack hAlign={isNarrow ? "end" : "between"} gap={2}>
        {!isNarrow && (
          <Text type="supporting" color="secondary">{t("Fichiers texte uniquement. Les tokens comptent sur ton budget.")}</Text>
        )}
        <HStack gap={2}>
          <Button
            label={t("Snippets")}
            variant="ghost"
            size="sm"
            icon={<Icon icon={BookmarkIcon} size="sm" />}
            onClick={() => setSnippetsOpen(true)}
          />
          <Button
            label={t("Compétences")}
            variant="ghost"
            size="sm"
            icon={<Icon icon={BoltIcon} size="sm" />}
            onClick={() => setSkillCreatorOpen(true)}
          />
          {messages.length > 0 && (
            <Button
              label={t("Fenêtre de contexte")}
              variant="ghost"
              size="sm"
              isIconOnly
              icon={<ContextRing used={used} max={max} />}
              onClick={() => setCtxOpen(true)}
            />
          )}
        </HStack>
      </HStack>
    </VStack>
  );

  return (
    <Layout
      height="fill"
      padding={6}
      header={
        <LayoutHeader hasDivider>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={0}>
              <Heading level={2}>{t("Playground")}</Heading>
              <Text type="supporting" color="secondary">{t("Discute en direct avec un modèle actif — réglages avancés, fichiers joints, réponses en streaming, sur ton budget de compte.")}</Text>
            </VStack>
            <HStack gap={2}>
              <Button
                label={t("Nouvelle conversation")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isIconOnly
                // `newConversation` bails out while an answer is generating: a
                // active button with a silent click made it look like a failure. We say
                // the unavailability instead of ignoring it — like « Nouvel onglet ».
                isDisabled={streaming}
                onClick={newConversation}
              />
              <Button
                label={t("Historique")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={ClockIcon} size="sm" />}
                onClick={() => setHistoryOpen(true)}
              />
              <Button
                label={t("Exporter")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                isIconOnly
                onClick={() => exportConversation({ title: convTitleFallback(messages, t("Conversation")), model, messages }, "md")}
              />
              <Button
                label={shared ? t("Lien copié") : t("Partager")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={LinkIcon} size="sm" />}
                isIconOnly
                isDisabled={!currentId}
                onClick={() => currentId && shareConversation(currentId)}
              />
              {/* Réglages : panneau ancré SOUS la roue crantée, en overlay
                  fixe — la page ne bouge pas (voir toggleSettings). */}
              <Button
                label={t("Réglages")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                isIconOnly
                onClick={(e) => toggleSettings(e.currentTarget)}
              />
              <Button
                label={t("Titrer automatiquement")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={SparklesIcon} size="sm" />}
                isIconOnly
                isDisabled={busyTitle || !currentId || !messages.length}
                onClick={genTitle}
              />
              <Button
                label={t("Résumer")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={DocumentTextIcon} size="sm" />}
                isIconOnly
                isDisabled={!messages.length}
                onClick={genSummary}
              />
              <Button
                label={t("Contexte")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={DocumentMagnifyingGlassIcon} size="sm" />}
                isIconOnly
                onClick={() => setCtxOpen(true)}
              />
            </HStack>
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent padding={0} isScrollable={false}>
          {/* VStack (flex column) gives ChatLayout the flex parent its own flex:1
              needs to fill the remaining height — LayoutContent renders display:block,
              so without this wrapper ChatLayout's flex:1 is a no-op.
              No scrollRef → ChatLayout is self-scrolling: its OWN root becomes the
              overflow:auto container and its dock uses position:sticky. Sticky is
              what actually keeps the composer pinned to the bottom during scroll —
              fixed-via-transform (tried earlier) turns out to behave like absolute
              positioning for descendants inside the same scrolling flow, so it drifts
              upward as the container scrolls (confirmed by instrumenting the DOM
              mid-stream). Since ChatLayout's own root is now full width (no
              `contentWidth` on the outer Layout — that's what narrowed it before and
              pushed the scrollbar to the middle of the page), its native scrollbar
              lands at the true right edge; density="spacious" narrows just the
              message column and composer to a shared 800px reading width. */}
          <HStack height="100%">
          <StackItem size="fill">
          <VStack height="100%">
          {/* Onglets : plusieurs conversations ouvertes. Basculement désactivé
              pendant un flux (l'état live est celui de la génération en cours). */}
          {tabs.length > 0 && (
            <VStack gap={1} padding={2}>
              <HStack gap={2} vAlign="center" wrap="wrap">
                {tabs.map((tb) => (
                  <HStack key={tb.id} gap={1} vAlign="center">
                    <Button
                      label={tb.title || t("Nouvelle conversation")}
                      variant={tb.id === activeTabId ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() => switchTab(tb.id)}
                    />
                    <Button
                      label={t("Fermer")}
                      variant="ghost"
                      size="sm"
                      // Closing a tab mid-generation was refused
                      // silently by `closeTab`: we show it.
                      isDisabled={streaming}
                      isIconOnly
                      icon={<Icon icon={XMarkIcon} size="sm" />}
                      onClick={() => closeTab(tb.id)}
                    />
                  </HStack>
                ))}
                <Button
                  label={t("Nouvel onglet")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  isDisabled={streaming}
                  onClick={newTab}
                />
              </HStack>
            </VStack>
          )}
          {isFirstEmpty ? (
            <StackItem size="fill">
              <VStack width="100%" height="100%" vAlign="center" hAlign="center" gap={4} padding={4}>
                <HStack gap={2} vAlign="center">
                  <Icon icon={SparklesIcon} size="lg" color="accent" />
                  <Text type="display-2" as="h1">{greeting}</Text>
                </HStack>
                <HStack gap={4} width="100%" hAlign="center">
                  <VStack maxWidth={720} width="100%">{composerNode}</VStack>
                </HStack>
              </VStack>
            </StackItem>
          ) : (
          <ChatLayout
            density="spacious"
            composer={composerNode}
            ref={suitLeBasSetRef}
            scrollButton={
              montrerDescendre ? (
                <Button
                  label={t("Descendre")}
                  variant="secondary"
                  size="sm"
                  onClick={descendreToutEnBas}
                />
              ) : null
            }>
            <ChatMessageList
              emptyState={
                <VStack gap={2} hAlign="center">
                  <Text type="display-2" as="h1">{greeting}</Text>
                  <Text type="supporting" color="secondary">{t("Écris ton message ci-dessous pour commencer.")}</Text>
                </VStack>
              }>
                {messages.map((m, i) => {
                  // Hidden messages (e.g. answers submitted from a question card)
                  // are sent to the model but never shown in the chat.
                  if (m.hidden) return null;
                  const isLast = i === messages.length - 1;
                  const streamingThis = streaming && isLast;
                  const isThinking = streamingThis && m.role === "assistant" && !m.content && !m.reasoning;
                  const prevAttachments = messages[i - 1]?.attachmentCount;
                  const canRegenerateThis = m.role === "assistant" && isLast && !streaming;
                  // Once a reply finishes, detect artifacts (code files / long
                  // documents) so they get a card in the bubble + the copyable panel.
                  // A clarifying question the model asked (rendered as clickable
                  // answers). Takes precedence over document/code artifact detection.
                  const ask = m.role === "assistant" && !streamingThis ? parseAsk(m.content) : null;
                  // FINISHED files become cards right away, without
                  // waiting for the answer to end: parseArtifacts only extracts
                  // blocks whose closing fence has arrived, the one in progress stays
                  // aside (panel) and not in the chat. `allowDoc` stays reserved
                  // for the end: flipping the whole message into a document mid-stream
                  // would make the already read text disappear.
                  // The file being written goes into the panel: we
                  // remove it once and for all from the analyzed content, so it
                  // comes out neither as a card nor in the chat prose.
                  const contenuAffiche =
                    streamingThis && isLast && liveCode ? m.content.slice(0, liveCode.start) : m.content;
                  const arts = m.role === "assistant" && !ask
                    ? parseArtifacts(
                        // Never-closed fence: we close it, otherwise the
                        // cut file stays as raw code in the middle of the bubble.
                        streamingThis ? contenuAffiche : contenuCloture(contenuAffiche),
                        !streamingThis && isDocTask(messages[i - 1]?.content ?? ""), t)
                    : null;
                  // Resume message: it only carries the end of the file.
                  const suite = m.role === "assistant" && !streamingThis
                    ? fusionDuMessage(messages, i, t)
                    : [];
                  // A message may contain only EDITS: the file
                  // to show is then the result, not what the message contains.
                  const modifs = m.role === "assistant" && !streamingThis
                    ? appliquerEdits(messages, i, t)
                    : { fichiers: [], echecs: [] };
                  // The model returned « la partie corrigée » instead of the file.
                  const fragments = m.role === "assistant" && !streamingThis
                    ? fragmentsDuMessage(messages, i, t)
                    : [];
                  // The stream broke AFTER delivering text: this is not a
                  // heuristic, it is an observed error. Without this flag, the
                  // answer stopped mid-word with nothing to explain it.
                  const coupeReseau = !!m.isError && m.role === "assistant"
                    && m.content.length > 200 && !streamingThis;
                  // A RESUME message only shows the rebuilt file. If
                  // there was nothing to complete, it shows no file: its
                  // content is the continuation of a text, not a deliverable. Without this test,
                  // the « fichier-2.txt » card (half a script) came back into the thread.
                  const estSuite = m.role === "assistant" && !streamingThis
                    && estReprise(messages[i - 1]);
                  const items = estSuite
                    ? [...suite, ...modifs.fichiers]
                    : [...(arts?.artifacts ?? []), ...modifs.fichiers];
                  // A file ending with </html> but whose script does not compile
                  // is unusable: nothing reported it, the page stayed blank.
                  const fichierCasse = !streamingThis
                    ? items.find((a) => a.kind === "code" && scriptCasse(a.content))
                    : undefined;
                  // When the model puts everything in the artifact and writes nothing
                  // outside, still show a short line in the chat (not an empty bubble).
                  const emptyMsg = items.some((a) => a.kind === "doc")
                    ? t("Voici le document — ouvre-le pour le lire ou le copier.")
                    : t("Voici le fichier — ouvre-le pour le copier.");
                  // While streaming, hide a half-written ```ask block (raw JSON) —
                  // the question UI appears once the block is complete.
                  // What is left to display in the chat during the stream: neither the
                  // half-written ```ask block (raw JSON), nor the file being
                  // written — the latter is written in the panel.
                  const streamingBody =
                    streamingThis && m.content.includes("```ask")
                      ? m.content.split("```ask")[0]
                      : streamingThis && m.content.includes("```edit")
                        ? m.content.split("```edit")[0]
                        : contenuAffiche;
                  // With a question card, show only the model's short intro
                  // sentence (its first line) — never the questions/options text,
                  // which live in the interactive card.
                  const askIntro = ask ? (ask.prose.split("\n").map((s) => s.trim()).find(Boolean) ?? "").slice(0, 280) : "";
                  // The ```edit block itself has no business in the chat: we
                  // keep the explanation sentence, the result goes to a card.
                  // The ```edit block is ALWAYS removed from the chat, even when it
                  // could not be applied: it is protocol, not an answer. In
                  // case of failure, the banner below explains it.
                  const contientEdit = m.content.includes("```edit");
                  // A resume has no prose to show: all its content is
                  // the end of the file, already glued into the card.
                  const proseHorsEdit = estSuite && suite.length
                    ? ""
                    : (arts?.prose ?? m.content)
                        .replace(/```edit[\s\S]*?(?:```|$)/g, "")
                        .trim();
                  const bodyText = ask
                    ? askIntro
                    : items.length
                      ? (proseHorsEdit || emptyMsg)
                      : contientEdit && !streamingThis
                        ? (proseHorsEdit || t("Modification proposée."))
                        : streamingBody;
                  // Block present but nothing applied and nothing reported: the parser
                  // could not read it at all. Must be said, otherwise the answer looks empty.
                  const editIllisible =
                    contientEdit && !streamingThis && !modifs.fichiers.length && !modifs.echecs.length;
                  // A document being streamed shows only a live-updating card in
                  // the chat (its raw text streams into the side panel instead).
                  // On a narrow screen there is no panel: the card would hide
                  // the very text being written, so we let it flow in the chat —
                  // exactly like a code file.
                  const streamingDoc =
                    streamingThis && m.role === "assistant" && m.content.length > 0 && !isNarrow &&
                    isDocTask(messages[i - 1]?.content ?? "");
                  return (
                  <ChatMessage key={i} sender={m.role}>
                    <ChatMessageBubble
                      variant={m.role === "user" ? "filled" : "ghost"}
                      className={m.role === "user" ? "bulle-question" : undefined}
                      /* Le nom du modèle au-dessus du contenu, comme LM Studio
                         (« google/gemma-4-e4b » sous la question) : il dit QUI
                         parle, et il marque le début de la réponse. */
                      name={
                        m.role === "assistant" ? (
                          <Text type="supporting" color="secondary">{model}</Text>
                        ) : undefined
                      }
                      metadata={
                        !isThinking && m.ts ? (
                          <ChatMessageMetadata
                            timestamp={<Timestamp value={m.ts} format="time" />}
                            status={m.isError ? "error" : undefined}
                            footer={
                              m.role === "assistant" && (m.tokens || m.tokensPerSec || canRegenerateThis || (streamingThis && liveStats)) ? (
                                <HStack gap={1} vAlign="center">
                                  <Text type="supporting" color="secondary">
                                    {streamingThis && liveStats
                                      ? // During the stream: counted on the SSE deltas, hence « ~ ».
                                        // An automatic resume is announced: otherwise the answer
                                        // seems to come out of nowhere.
                                        (reprise
                                          ? `${t("Reprise automatique")} ${reprise}/${MAX_REPRISES_AUTO} · `
                                          : "") + `~${liveStats.tokens} tokens · ${liveStats.tps} tok/s`
                                      : [
                                          m.tokens ? `${m.tokens} tokens` : null,
                                          m.tokensPerSec ? `${m.tokensPerSec} tok/s` : null,
                                          m.ttft ? `TTFT ${m.ttft}s` : null,
                                        ]
                                          .filter(Boolean)
                                          .join(" · ")}
                                  </Text>
                                  <Button
                                    label={t("Copier")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                    onClick={() => void copierTexte(m.content, () =>
                                      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                                  />
                                  {canRegenerateThis && (
                                    <Button
                                      label={t("Régénérer")}
                                      variant="ghost"
                                      size="sm"
                                      isIconOnly
                                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                                      onClick={regenerate}
                                    />
                                  )}
                                </HStack>
                              ) : m.role === "user" ? (
                                <HStack gap={1} vAlign="center">
                                  {editingIdx === i && (
                                    <StatusDot variant="accent" isPulsing label={t("En modification")} />
                                  )}
                                  <Button
                                    label={t("Éditer")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={PencilIcon} size="sm" />}
                                    onClick={() => editMessage(i)}
                                  />
                                  <Button
                                    label={t("Copier")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                    onClick={() => void copierTexte(m.content, () =>
                                      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                                  />
                                </HStack>
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {m.role === "user" && m.images?.length ? (
                        <HStack gap={1} wrap="wrap">
                          {m.images.map((src, k) => (
                            <Thumbnail
                              key={k}
                              src={src}
                              alt={t("Image jointe {n}").replace("{n}", String(k + 1))}
                              label={t("Image jointe {n}").replace("{n}", String(k + 1))}
                              onClick={() => setImageVue({ srcs: m.images ?? [], index: k })}
                            />
                          ))}
                        </HStack>
                      ) : null}
                      {/* Outils en cours : on dit ce qui est cherché, lu ou
                          généré, au fil de l'eau. Sans ça l'attente est muette
                          pendant des dizaines de secondes. */}
                      {streamingThis && etapesWeb.length > 0 && (
                        <VStack gap={1} padding={2}>
                          {etapesWeb.map((e, k) => {
                            const l = libelleEtapeWeb(e, t);
                            const media = e.etape === "generation" ? "image"
                              : e.etape === "generation_video" ? "video" : null;
                            return (
                              <VStack key={`web-${k}`} gap={2}>
                                <HStack gap={2} vAlign="center">
                                  <StatusDot variant={l.fini ? "success" : "accent"}
                                    isPulsing={!l.fini} label={l.texte} />
                                  <Text type="supporting" color="secondary">{l.texte}</Text>
                                </HStack>
                                {/* Génération en cours : le MÊME pavé que la page
                                    Image (carré, photo) ou Vidéo (16/9, pellicule),
                                    pas un spinner — c'est lui qui occupera la place
                                    du résultat. UN PAVÉ PAR IMAGE annoncée (le champ
                                    `nombre` de l'étape) : quatre carrés gris pendant
                                    quatre images en cuisson, c'est ce que la page
                                    Image montre, et un seul carré se lisait comme
                                    « rien ne se passe ». */}
                                {!l.fini && media && (
                                  <HStack gap={2} wrap="wrap">
                                    {Array.from({ length: Math.max(1, Math.min(4, Number((e as { nombre?: number }).nombre) || 1)) })
                                      .map((_, p) => (
                                        <VStack key={`gen-${p}`} maxWidth={200} width="100%">
                                          <AspectRatio ratio={media === "image" ? 1 : 16 / 9} fit="contain">
                                            <GenerationPlaceholder media={media} />
                                          </AspectRatio>
                                        </VStack>
                                      ))}
                                  </HStack>
                                )}
                              </VStack>
                            );
                          })}
                        </VStack>
                      )}
                      {isThinking && etapesWeb.length === 0 ? (
                        <ThinkingIndicator fixedLabel={prevAttachments ? t("Lecture du fichier…") : undefined} />
                      ) : streamingDoc ? (
                        <ClickableCard
                          label={t("Ouvrir le document en cours de rédaction")}
                          variant="muted"
                          onClick={openLiveDoc}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                            <VStack gap={0}>
                              <Text weight="semibold">{docTitleFromContent(m.content)}</Text>
                              <Text type="supporting" color="secondary">{t("Rédaction en cours…")}</Text>
                            </VStack>
                          </HStack>
                        </ClickableCard>
                      ) : ask ? (
                        <VStack gap={2}>
                          <ReasoningBlock reasoning={m.reasoning || ""} ms={m.reasoningMs} />
                          {bodyText.trim() ? <Markdown>{bodyText}</Markdown> : null}
                          <AskQuestion
                            questions={ask.questions}
                            answered={!isLast}
                            onSubmit={(ans) =>
                              answer(
                                ask.questions.length === 1
                                  ? ans[0]
                                  : ask.questions.map((q, k) => `${q.question}\n→ ${ans[k]}`).join("\n\n"),
                              )
                            }
                          />
                        </VStack>
                      ) : (
                        <VStack gap={2}>
                          <ReasoningBlock reasoning={m.reasoning || ""} ms={m.reasoningMs} streaming={streamingThis && !bodyText.trim()} />
                          {/* Le fondu court du PREMIER mot de la réponse, quand
                              il y a eu une réflexion : c'est le moment exact où
                              le bloc de réflexion se replie — la frontière
                              réflexion → écriture, enfin visible. */}
                          <Markdown
                            className={m.reasoning && bodyText.trim() ? "apparition-ecriture" : undefined}
                            isStreaming={streamingThis}>
                            {bodyText || " "}
                          </Markdown>
                          {/* Une modification dont l'ancre n'existe pas dans le
                              fichier ne s'applique PAS. On le dit, plutôt que de
                              laisser croire que la correction est faite. */}
                          {fragments.length > 0 && (
                            <Banner
                              status="warning"
                              title={t("Seul un extrait a été renvoyé")}
                              description={t(
                                "Le modèle a donné la partie corrigée, pas le fichier complet. La version précédente reste ouverte dans le volet.",
                              )}
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Demander le fichier complet")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={() => demanderFichierComplet(fragments[0])}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {(modifs.echecs.length > 0 || editIllisible) && (
                            <Banner
                              status="warning"
                              title={t("Modification non appliquée")}
                              description={
                                editIllisible
                                  ? t("La modification proposée n'a pas pu être lue. Redemande la correction, ou demande le fichier complet.")
                                  : t("Le texte à remplacer n'a pas été retrouvé dans le fichier. Demande la correction en précisant l'endroit, ou demande le fichier complet.")
                              }
                            />
                          )}
                          {fichierCasse && (
                            <Banner
                              status="warning"
                              title={t("Fichier inutilisable")}
                              description={t("Le fichier se termine bien, mais son JavaScript ne compile pas — un bloc n'est jamais refermé, donc la page reste vide.")}
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Refaire le fichier")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={() => refaireFichier(fichierCasse.title)}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {/* Coupé par le plafond de tokens : sans ce message, la
                              réponse s'arrête en plein mot et rien ne l'explique. */}
                          {(m.truncated || coupeReseau || (!streamingThis && messageIncomplet(messages, i, t))) && !streamingThis && (
                            <Banner
                              status="warning"
                              title={t("Réponse coupée")}
                              description={
                                m.truncated
                                  ? t("Le plafond de tokens a été atteint. Reprends la suite, ou augmente « Max tokens » dans les réglages.")
                                  : coupeReseau
                                  ? t("La connexion s'est interrompue pendant la génération. Reprends la suite — ce qui est déjà écrit est conservé.")
                                  : t("Le fichier s'arrête avant sa fin et les reprises automatiques n'ont pas suffi. Relance la suite, ou demande-lui de l'écrire en plusieurs fichiers.")
                              }
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Continuer")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={continuer}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {items.map((a, ai) => (
                            <ClickableCard
                              key={ai}
                              label={a.kind === "code" ? `${t("Ouvrir le fichier")} ${renamedTitle(a)}` : t("Ouvrir le document")}
                              variant="muted"
                              onClick={() => { setArtifact(a); setRenamingArtifact(false); }}>
                              <HStack gap={2} vAlign="center">
                                <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                                <VStack gap={0}>
                                  <Text weight="semibold">{renamedTitle(a)}</Text>
                                  <Text type="supporting" color="secondary">
                                    {a.kind === "code" ? a.lang : t("Ouvrir et copier dans le volet")}
                                  </Text>
                                </VStack>
                              </HStack>
                            </ClickableCard>
                          ))}
                        </VStack>
                      )}
                    </ChatMessageBubble>
                  </ChatMessage>
                  );
                })}
            </ChatMessageList>
          </ChatLayout>
          )}
          </VStack>
          </StackItem>
          {(artifact || showLive || dernierFini) && !isNarrow && (
            <>
              <ResizeHandle
                direction="horizontal"
                resizable={artifactResize.props}
                isReversed
                pillPlacement="start"
                hasDivider
                label={t("Redimensionner le panneau")}
              />
              <Card
                variant="transparent"
                height="100%"
                style={{ width: artifactResize.size, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
                <Toolbar
                  label={panelIsCode ? t("Fichier") : t("Document")}
                  dividers={["bottom"]}
                  startContent={
                    renamingArtifact && canRenamePanel ? (
                      <TextInput
                        label={t("Nouveau nom du fichier")}
                        value={renameArtifactValue}
                        onChange={setRenameArtifactValue}
                        onEnter={() => epingle && commitArtifactRename(epingle, renameArtifactValue)}
                        isLabelHidden
                        size="sm"
                      />
                    ) : (
                      <HStack gap={2} vAlign="center">
                        <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                        <VStack gap={0}>
                          <Text weight="semibold">{panelTitle}</Text>
                          {panelSubtitle ? <Text type="supporting" color="secondary">{panelSubtitle}</Text> : null}
                        </VStack>
                      </HStack>
                    )
                  }
                  endContent={
                    renamingArtifact && canRenamePanel ? (
                      <>
                        <Button label={t("Valider")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={CheckIcon} size="sm" />}
                          onClick={() => epingle && commitArtifactRename(epingle, renameArtifactValue)} />
                        <Button label={t("Annuler")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={XMarkIcon} size="sm" />}
                          onClick={() => setRenamingArtifact(false)} />
                      </>
                    ) : (
                      <>
                        {canRenamePanel && (
                          <Button label={t("Renommer ce fichier")} variant="ghost" size="sm" isIconOnly
                            icon={<Icon icon={PencilIcon} size="sm" />}
                            onClick={() => { setRenameArtifactValue(panelTitle); setRenamingArtifact(true); }} />
                        )}
                        <Button label={t("Plein écran")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowsPointingOutIcon} size="sm" />}
                          onClick={() => setPlein(true)} />
                        <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                          onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                        <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                          onClick={() => void copierTexte(panelContent, () =>
                            showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))} />
                        <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={XMarkIcon} size="sm" />}
                          onClick={() => { setArtifact(null); setLiveDocOpen(false); liveDocOpenRef.current = false; }} />
                      </>
                    )
                  }
                />
                {panelEstHtml && (
                  <HStack padding={3}>
                    <SegmentedControl
                      label={t("Affichage")}
                      value={htmlPreview ? "apercu" : "code"}
                      onChange={(v) => setHtmlPreview(v === "apercu")}
                      size="sm">
                      <SegmentedControlItem value="apercu" label={t("Aperçu")} />
                      <SegmentedControlItem value="code" label={t("Code source")} />
                    </SegmentedControl>
                  </HStack>
                )}
                {panelEstHtml && htmlPreview && erreurApercu ? (
                  /* The page opens but raises an error: it is well-formed and
                     yet unusable. Without this banner, the user sees an
                     empty or frozen preview with no idea why. */
                  <HStack padding={3}>
                    <Banner
                      status="warning"
                      title={t("La page ne s'exécute pas")}
                      description={erreurApercu}
                      endContent={
                        <Button
                          label={t("Corriger")}
                          variant="primary"
                          size="sm"
                          isDisabled={streaming}
                          onClick={() => corrigerErreur(panelTitle, erreurApercu)}
                        />
                      }
                    />
                  </HStack>
                ) : null}
                {panelEstHtml && htmlPreview ? (
                  /* Model-generated page, in an ISOLATED iframe.
                     `allow-scripts` WITHOUT `allow-same-origin`: the page can
                     run — otherwise buttons and interactions are dead, which
                     makes the preview useless for an interactive page — but its
                     origin stays OPAQUE. It can therefore neither read session
                     cookies, nor touch the DOM of the hosting page, nor
                     call the API with the user's rights.
                     `allow-same-origin` must NEVER be added here: combined with
                     `allow-scripts`, it cancels the sandbox and a generated HTML
                     would become code executed in our own origin. */
                  <iframe
                    ref={refApercu}
                    title={panelTitle}
                    src={previewUrl}
                    sandbox="allow-scripts allow-forms allow-popups"
                    style={{
                      flex: 1,
                      minHeight: 0,
                      width: "100%",
                      border: "none",
                      background: "var(--color-background-surface)",
                    }}
                  />
                ) : (
                <VStack ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable style={{ flex: 1, minHeight: 0 }}>
                  {/* Valeurs unifiées : pendant le flux il n'y a pas encore
                      d'artefact épinglé, mais bien un fichier à afficher. */}
                  {/* `flexShrink: 0` : SANS ça le CodeBlock rétrécit à la
                      hauteur du corps (ses deux étages sont des items flex en
                      `flex: 0 1 auto`, `min-height` résolu à 0 puisque leur
                      `overflow` n'est pas `visible`) et son PROPRE conteneur
                      interne devient le seul à défiler — le corps, lui, ne
                      déborde jamais : le suivi automatique et le bouton
                      « Descendre », branchés sur le corps, ne servaient à
                      rien. Empêcher le rétrécissement rend au corps sa place
                      d'UNIQUE conteneur de défilement, pour le code comme pour
                      les documents (mesuré : corps 792/792 contre code interne
                      5 716/720). */}
                  {panelIsCode
                    ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped container="section" style={{ flexShrink: 0 }} />
                    : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                </VStack>
                )}
                {showPanelJump && (
                  <HStack style={{ position: "absolute", bottom: "var(--spacing-4)", left: "50%", transform: "translateX(-50%)", zIndex: 2 }}>
                    <Button label={t("Descendre")} variant="primary" size="sm"
                      icon={<Icon icon={ArrowDownIcon} size="sm" />}
                      onClick={panelJumpDown} />
                  </HStack>
                )}
              </Card>
            </>
          )}
          {/* Historique : UNE ligne par conversation, corbeille au bout. Cliquer la
              ligne ouvre la conversation (et referme le panneau) ; cliquer la
              corbeille supprime SANS refermer, pour pouvoir faire le ménage
              d'affilée. La corbeille est un bouton FRÈRE de la ligne, pas un
              bouton imbriqué : aucun risque qu'un clic déclenche les deux. */}
          {/* Réglages du playground : overlay FIXE ancré sous la roue crantée.
              Pas de focus trap, pas d'élément caché à révéler : ouvrir ne
              déclenche AUCUN défilement — le chat reste exactement où il est. */}
          {isSettingsOpen && settingsPos && (
            <HStack
              className="playground-settings-panel"
              style={{ position: "fixed", top: settingsPos.top, right: settingsPos.right,
                       width: settingsPos.largeur, zIndex: 30 }}
            >
              <Card style={{ width: "100%" }}>
                <VStack gap={2}>
                  <HStack vAlign="center">
                    <StackItem size="fill">
                      <Text weight="semibold">{t("Réglages du playground")}</Text>
                    </StackItem>
                    <Button
                      label={t("Fermer")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={XMarkIcon} size="sm" />}
                      onClick={() => setIsSettingsOpen(false)}
                    />
                  </HStack>
                  <VStack gap={3} style={{ overflowY: "auto", maxHeight: settingsPos.maxH }} isScrollable>
                    <SettingsPanel
                      settings={settings}
                      onChange={setSettings}
                      contexte={modelLimits[model]}
                      provenance={systemProvenance}
                      onProvenance={setSystemProvenance}
                    />
                  </VStack>
                </VStack>
              </Card>
            </HStack>
          )}
          <Dialog isOpen={historyOpen} onOpenChange={(o) => { if (!o) setHistoryOpen(false); }} width={560}>
            <DialogHeader
              title={t("Historique")}
              subtitle={`${conversations.length} ${t("conversations")}`}
              hasDivider
              onOpenChange={(o) => { if (!o) setHistoryOpen(false); }}
            />
            <VStack padding={3} gap={2}>
              <TextInput
                label={t("Rechercher")}
                isLabelHidden
                size="sm"
                value={histQuery}
                onChange={setHistQuery}
                placeholder={t("Rechercher une conversation")}
              />
              <VStack gap={1} height={470} isScrollable>
              {visibleConvs.length === 0 ? (
                <Text color="secondary">{q ? t("Aucun résultat") : t("Aucune conversation")}</Text>
              ) : (
                visibleConvs.map((conv) =>
                  renamingId === conv.id ? (
                    <HStack key={conv.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <TextInput
                          label={t("Renommer")}
                          isLabelHidden
                          value={renameValue}
                          onChange={setRenameValue}
                          size="sm"
                          hasAutoFocus
                          onEnter={() => commitRename(conv.id)}
                        />
                      </StackItem>
                      <Button label={t("Valider")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={CheckIcon} size="sm" />} onClick={() => commitRename(conv.id)} />
                      <Button label={t("Annuler")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} onClick={() => setRenamingId(null)} />
                    </HStack>
                  ) : (
                    <HStack key={conv.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <ClickableCard
                          label={conv.title || t("Conversation")}
                          variant={conv.id === currentId ? "default" : "muted"}
                          onClick={() => { selectConversation(conv); setHistoryOpen(false); }}>
                          <VStack gap={0}>
                            <Text maxLines={1}>{conv.title || t("Conversation")}</Text>
                            <Text type="supporting" color="secondary">
                              <Timestamp value={conv.ts} format="date_time" />
                            </Text>
                          </VStack>
                        </ClickableCard>
                      </StackItem>
                      <Button
                        label={pinnedIds.includes(conv.id) ? t("Désépingler") : t("Épingler")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={pinnedIds.includes(conv.id) ? StarSolidIcon : StarIcon} size="sm" color={pinnedIds.includes(conv.id) ? "accent" : "inherit"} />}
                        onClick={() => togglePinned(conv.id)}
                      />
                      <Button
                        label={t("Renommer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={PencilIcon} size="sm" />}
                        onClick={() => startRename(conv.id, conv.title)}
                      />
                      <Button
                        label={t("Exporter JSON")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                        onClick={() => exportConversation({ title: conv.title || t("Conversation"), model: conv.model || "", messages: conv.messages }, "json")}
                      />
                      <Button
                        label={t("Partager")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={LinkIcon} size="sm" />}
                        onClick={() => shareConversation(conv.id)}
                      />
                      <Button
                        label={t("Supprimer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={TrashIcon} size="sm" />}
                        onClick={() => deleteConversation(conv.id)}
                      />
                    </HStack>
                  ),
                )
              )}
              </VStack>
            </VStack>
          </Dialog>
          <Dialog isOpen={snippetsOpen} onOpenChange={(o) => { if (!o) setSnippetsOpen(false); }} width={480}>
            <DialogHeader
              title={t("Snippets")}
              subtitle={t("Prompts réutilisables")}
              hasDivider
              onOpenChange={(o) => { if (!o) setSnippetsOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              <Button
                label={t("Enregistrer le prompt courant en snippet")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isDisabled={!input.trim()}
                onClick={saveSnippet}
              />
              {snippets.length === 0 ? (
                <Text color="secondary">{t("Aucun snippet")}</Text>
              ) : (
                <VStack gap={1} height={380} isScrollable>
                  {snippets.map((s) => (
                    <HStack key={s.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <ClickableCard label={s.label} variant="muted" onClick={() => insertSnippet(s.content)}>
                          <Text maxLines={2} type="supporting" color="secondary">{s.content}</Text>
                        </ClickableCard>
                      </StackItem>
                      <Button
                        label={t("Supprimer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={TrashIcon} size="sm" />}
                        onClick={() => deleteSnippet(s.id)}
                      />
                    </HStack>
                  ))}
                </VStack>
              )}
            </VStack>
          </Dialog>
          <SkillCreator
            open={skillCreatorOpen}
            onOpenChange={(o) => {
              setSkillCreatorOpen(o);
              if (!o) setEditingSkill(null);
            }}
            initial={editingSkill}
            customSkills={customSkills}
            onSave={(s) => (editingSkill ? updateCustomSkill(s) : addCustomSkill(s))}
            onDelete={deleteCustomSkill}
          />
          <Dialog isOpen={summaryOpen} onOpenChange={(o) => { if (!o) setSummaryOpen(false); }} width={560}>
            <DialogHeader
              title={t("Résumé de la conversation")}
              hasDivider
              onOpenChange={(o) => { if (!o) setSummaryOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              <Text type="supporting" color="secondary">{summary}</Text>
              <HStack hAlign="end" gap={2}>
                <Button
                  label={t("Copier")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                  onClick={() => void copierTexte(summary, () =>
                    showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                />
              </HStack>
            </VStack>
          </Dialog>
          <Dialog isOpen={ctxOpen} onOpenChange={(o) => { if (!o) setCtxOpen(false); }} width={560}>
            <DialogHeader
              title={t("Fenêtre de contexte")}
              subtitle={t("Ce que le modèle voit pour ce tour")}
              hasDivider
              onOpenChange={(o) => { if (!o) setCtxOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              {/* Total + barre de progression */}
              <VStack gap={1}>
                <HStack hAlign="between" vAlign="center">
                  <Text weight="semibold">{t("Utilisation du contexte")}</Text>
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {fmtK(used)} / {fmtK(max)} {t("tokens")}
                  </Text>
                </HStack>
                <ProgressBar label={t("Utilisation du contexte")} isLabelHidden value={used} max={max} variant={ctxLevel} />
              </VStack>
              {/* Entrée / sortie : le contexte n'est pas un bloc opaque — on sépare
                  ce qui le remplit (prompt) de ce qui est produit (réponse). */}
              <VStack gap={1}>
                <Text type="label">{t("Entrée / sortie")}</Text>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="accent" label={t("Entrée (prompt)")} />
                  <Text type="supporting">{t("Entrée (prompt)")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{inTokens.toLocaleString(numLocale)} {t("tokens")}</Text>
                </HStack>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="neutral" label={t("Sortie (généré)")} />
                  <Text type="supporting">{t("Sortie (généré)")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {ioTokens ? `${outTokens.toLocaleString(numLocale)} ${t("tokens")}` : "—"}
                  </Text>
                </HStack>
              </VStack>
              {/* Répartition : prompt système vs messages/fichiers */}
              <VStack gap={1}>
                <Text type="label">{t("Répartition")}</Text>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="accent" label={t("System prompt")} />
                  <Text type="supporting">{t("System prompt")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{sysPct} %</Text>
                </HStack>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="neutral" label={t("Messages")} />
                  <Text type="supporting">{t("Messages")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{msgPct} %</Text>
                </HStack>
              </VStack>
              <HStack gap={2} vAlign="center" wrap="wrap">
                {model && <Badge label={model} variant="info" />}
                <Badge label={`${Math.round((used / max) * 100)} % ${t("contexte")}`} variant="info" />
                {convTokens > 0 && (
                  <Badge label={`${convTokens.toLocaleString(numLocale)} ${t("tokens")}`} variant="info" />
                )}
              </HStack>
              {settings.system ? (
                <VStack gap={1}>
                  <Text type="label">{t("System prompt")}</Text>
                  <Text type="supporting" color="secondary">{settings.system}</Text>
                </VStack>
              ) : (
                <Text type="supporting" color="secondary">{t("Aucun system prompt.")}</Text>
              )}
              <VStack gap={1}>
                <Text type="label">{t("Fichiers joints")}</Text>
                {attachments.length === 0 ? (
                  <Text type="supporting" color="secondary">{t("Aucun fichier.")}</Text>
                ) : (
                  attachments.map((f, i) => (
                    <HStack key={i} gap={2} vAlign="center">
                      <Text type="supporting" color="secondary">{f.name}</Text>
                      <Text type="supporting" color="secondary">
                        {f.image ? t("image") : `${Math.ceil(f.content.length / 1024)} Ko`}
                      </Text>
                    </HStack>
                  ))
                )}
              </VStack>
              <Text type="supporting" color="secondary">
                {t("La fenêtre de contexte = entrée (system prompt + messages + fichiers) + sortie (réponse générée). Le % indique la part utilisée.")}
              </Text>
            </VStack>
          </Dialog>
          {(artifact || showLive) && (isNarrow || plein) && (
            <Dialog isOpen onOpenChange={(o) => { if (!o) fermerPanneau(); }} variant="fullscreen">
              <Layout
                header={
                  <DialogHeader
                    title={panelTitle}
                    subtitle={panelSubtitle || undefined}
                    hasDivider
                    onOpenChange={(o) => { if (!o) fermerPanneau(); }}
                    endContent={
                      <HStack gap={1} vAlign="center">
                        {renamingArtifact && canRenamePanel ? (
                          <>
                            <Button label={t("Valider")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={CheckIcon} size="sm" />}
                              onClick={() => epingle && commitArtifactRename(epingle, renameArtifactValue)} />
                            <Button label={t("Annuler")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={XMarkIcon} size="sm" />}
                              onClick={() => setRenamingArtifact(false)} />
                          </>
                        ) : (
                          <>
                            {canRenamePanel && (
                              <Button label={t("Renommer ce fichier")} variant="ghost" size="sm" isIconOnly
                                icon={<Icon icon={PencilIcon} size="sm" />}
                                onClick={() => { setRenameArtifactValue(panelTitle); setRenamingArtifact(true); }} />
                            )}
                            <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                              onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                            <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                              onClick={() => void copierTexte(panelContent, () =>
                            showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))} />
                          </>
                        )}
                      </HStack>
                    }
                    startContent={
                      renamingArtifact && canRenamePanel ? (
                        <TextInput
                          label={t("Nouveau nom du fichier")}
                          value={renameArtifactValue}
                          onChange={setRenameArtifactValue}
                          onEnter={() => epingle && commitArtifactRename(epingle, renameArtifactValue)}
                          isLabelHidden
                          size="sm"
                        />
                      ) : panelEstHtml ? (
                        <SegmentedControl
                          label={t("Affichage")}
                          value={htmlPreview ? "apercu" : "code"}
                          onChange={(v) => setHtmlPreview(v === "apercu")}
                          size="sm">
                          <SegmentedControlItem value="apercu" label={t("Aperçu")} />
                          <SegmentedControlItem value="code" label={t("Code source")} />
                        </SegmentedControl>
                      ) : undefined
                    }
                  />
                }
                content={
                  panelEstHtml && htmlPreview ? (
                    /* Same isolated preview as in the panel: full screen is meant
                       precisely to WATCH the page, not re-read its code. */
                    <iframe
                      ref={refApercu}
                      title={panelTitle}
                      src={previewUrl}
                      sandbox="allow-scripts allow-forms allow-popups"
                      style={{ width: "100%", height: "100%", border: "none",
                               background: "var(--color-background-surface)" }}
                    />
                  ) : (
                  <LayoutContent ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable>
                    {panelIsCode
                      ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped container="section" style={{ flexShrink: 0 }} />
                      : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                  </LayoutContent>
                  )
                }
              />
              {showPanelJump && (
                <HStack style={{ position: "fixed", bottom: "var(--spacing-6)", left: "50%", transform: "translateX(-50%)", zIndex: 10 }}>
                  <Button label={t("Descendre")} variant="primary" size="sm"
                    icon={<Icon icon={ArrowDownIcon} size="sm" />}
                    onClick={panelJumpDown} />
                </HStack>
              )}
            </Dialog>
          )}
          </HStack>
        </LayoutContent>
      }
    />
  );
}
