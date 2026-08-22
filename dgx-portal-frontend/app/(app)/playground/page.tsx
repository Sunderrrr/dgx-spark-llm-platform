"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Toolbar } from "@astryxdesign/core/Toolbar";
import { useResizable, ResizeHandle } from "@astryxdesign/core/Resizable";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Selector } from "@astryxdesign/core/Selector";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Markdown } from "@astryxdesign/core/Markdown";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Token } from "@astryxdesign/core/Token";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Grid } from "@astryxdesign/core/Grid";
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
import {
  PaperClipIcon,
  Cog6ToothIcon,
  ArrowDownTrayIcon,
  ArrowDownIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  PencilIcon,
  PlusIcon,
  ClockIcon,
  TrashIcon,
  SparklesIcon,
  CodeBracketIcon,
  LightBulbIcon,
  DocumentMagnifyingGlassIcon,
  DocumentTextIcon,
  XMarkIcon,
  PaperAirplaneIcon,
  KeyIcon,
  ArrowsPointingOutIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { useSettingsDialog } from "@/lib/settings-dialog";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { DictateButton } from "../_components/DictateButton";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { fetchCsrfToken, fetchPlaygroundData, sendJSON, streamChat } from "@/lib/api";
import {
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
import { AskQuestion } from "./_components/AskQuestion";
import { ContextMeter } from "./_components/ContextMeter";
import { SettingsPanel } from "./_components/SettingsPanel";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";

const DEFAULT_SETTINGS: Settings = {
  system: "",
  temperature: 0.7,
  // Le maximum. Seuls les tokens RÉELLEMENT produits sont facturés, donc un
  // plafond haut ne coûte rien sur une réponse courte — et 4096 coupait net toute
  // réponse un peu longue (page HTML complète, gros fichier de configuration).
  // Le backend rabaisse cette valeur à ce qui reste dans la fenêtre de contexte
  // une fois le prompt compté : une longue conversation ne part donc pas en
  // erreur, elle obtient simplement une réponse plus courte.
  maxTokens: 131072,
  topP: 1,
  reasoning: false,
};

const ATTACH_ACCEPT =
  ".md,.markdown,.txt,.text,.log,.logs,.err,.error,.out,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.py,.js,.ts,.jsx,.tsx,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.bash,.sql,.html,.css,.xml,.diff,.patch";

const PRESETS = [
  {
    heading: "Code Python",
    body: "Génère une fonction, un script ou un test",
    prompt: "Écris une fonction Python qui vérifie si un nombre est premier.",
    icon: CodeBracketIcon,
  },
  {
    heading: "Expliquer",
    body: "Décompose un sujet technique simplement",
    prompt: "Explique la mémoire unifiée du DGX Spark en termes simples.",
    icon: LightBulbIcon,
  },
  {
    heading: "Analyser des logs",
    body: "Trouve la cause d'une erreur dans un extrait de logs",
    prompt: "Analyse ces logs et trouve la cause de l'erreur : ",
    icon: DocumentMagnifyingGlassIcon,
  },
  {
    heading: "Résumer",
    body: "Condense un texte en points clés",
    prompt: "Résume ce texte en 3 points : ",
    icon: DocumentTextIcon,
  },
];
const MAX_ATTACHMENT_BYTES = 96 * 1024;

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
- Only ask when it genuinely helps; otherwise just answer normally.`;

// Le modèle pose autant de questions qu'il le juge utile — deux ou dix. Ces
// plafonds ne sont PAS un cadrage éditorial mais un garde-fou : une génération
// qui déraille ne doit pas produire un questionnaire interminable.
const MAX_ASK_QUESTIONS = 20;
const MAX_ASK_OPTIONS = 8;

// One clarifying question + its proposed answers.
type AskQ = { question: string; options: string[] };
// A model's clarifying block: one or more questions, plus any prose around it.
type AskBlock = { questions: AskQ[]; prose: string };

/** Une modification ciblée d'un fichier déjà produit. */
type FileEdit = { file: string; find: string; replace: string };

// Instruction ajoutée au prompt système : corriger un fichier déjà produit sans
// le réécrire en entier. Réécrire 400 lignes pour en changer trois coûte du temps,
// des tokens, et réintroduit des erreurs ailleurs dans le fichier.
// Le modèle nomme lui-même ses fichiers : c'est lui qui sait ce que l'utilisateur
// a demandé et dans quel projet ça s'insère. Sans ça, l'interface doit deviner et
// retombe sur un nom générique.
const NAME_INSTRUCTION = `Name every file you output: put its path in backticks on the line just before the code block (\`index.html\`, \`roles/web/tasks/main.yml\`), chosen from what the user asked. Reuse the exact same name for a file you already produced.`;

const EDIT_INSTRUCTION = `When the user asks you to FIX or CHANGE a file you already produced in this conversation, do NOT output the whole file again. Output only the edits, as a single fenced block:
\`\`\`edit
{"edits": [{"file": "<exact file name you used before>", "find": "<exact text to replace, copied character for character from the file>", "replace": "<the new text>"}]}
\`\`\`
Rules for edits:
- \`find\` must appear EXACTLY ONCE in the current file, copied verbatim — including indentation. Include a few surrounding lines if needed to make it unique.
- Several edits are allowed in the same block; they are applied in order.
- Write one short sentence before the block saying what you changed. Never describe the change inside the block.
- Only rewrite the whole file when the change really is a rewrite (restructuring most of it), or when the file does not exist yet.
- NEVER output a shortened version of a file under its own name — no excerpt, no "rest unchanged", no "...". Either the edits block, or the file complete from first line to last.`;

/** Lit un bloc ```edit. Même tolérance que parseAsk : ces blocs sortent d'un
 *  modèle, ils arrivent parfois tronqués ou suivis de déchets. */
function parseEdits(content: string): FileEdit[] {
  const m = content.match(/```edit\s*\n([\s\S]*?)(?:```|$)/);
  if (!m) return [];
  try {
    const body = m[1].trim();
    let obj;
    try {
      obj = JSON.parse(body);
    } catch {
      // Trois défauts vus en vrai, dans cet ordre de fréquence : retours à la
      // ligne bruts dans une chaîne, déchets après la fin, objet non refermé.
      const repare = escapeRawControlChars(body);
      try {
        obj = JSON.parse(repare);
      } catch {
        obj = JSON.parse(firstJsonValue(repare) ?? balanceJson(repare));
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

// Referme un JSON tronqué en fin de chaîne. Un modèle ouvert de cette taille
// oublie régulièrement le dernier `}` ou `]` — un seul caractère manquant faisait
// échouer JSON.parse, et le bloc de questions retombait en JSON brut sous les yeux
// de l'utilisateur (constaté en production). On rééquilibre plutôt que d'abandonner.
function balanceJson(src: string): string {
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
  let out = src.replace(/,\s*$/, "");     // virgule en suspens avant la coupure
  if (inString) out += '"';                // chaîne laissée ouverte
  while (stack.length) out += stack.pop() === "{" ? "}" : "]";
  return out;
}

/** Coupe ce que le modèle a écrit APRÈS son bloc de questions.
 *
 * L'instruction lui demande de s'arrêter au bloc, mais il lui arrive de repartir
 * et de générer jusqu'au plafond de tokens (constaté : 4096 pour une question de
 * ~150). Ce texte n'est pas affiché — il se retrouve quand même dans l'historique
 * renvoyé au modèle au tour suivant, qui répond alors n'importe quoi ou rien.
 * On garde l'introduction et le bloc, on jette la suite.
 */
function trimAfterAsk(content: string): string {
  const i = content.indexOf("```ask");
  if (i < 0) return content;
  const close = content.indexOf("```", i + 6);
  return close < 0 ? content : content.slice(0, close + 3);
}

/** Échappe les caractères de contrôle laissés BRUTS dans une chaîne JSON.
 *
 * Un modèle qui écrit du code dans un champ « replace » oublie régulièrement
 * d'échapper ses retours à la ligne : la chaîne contient un vrai saut de ligne,
 * ce que JSON interdit, et tout le bloc devient illisible. Constaté en
 * production sur un bloc d'édition de plusieurs dizaines de lignes.
 */
function escapeRawControlChars(src: string): string {
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

/** La première valeur JSON complète de la chaîne, ignorant ce qui suit.
 *
 * Le modèle ajoute parfois un caractère APRÈS l'objet fermé (constaté : un
 * guillemet orphelin, « …]}\" »). JSON.parse refuse tout ce qui suit une valeur
 * complète, et le rééquilibrage ne sert à rien ici : le JSON n'est pas tronqué,
 * il est suivi de déchets. On coupe donc dès que la profondeur revient à zéro.
 */
function firstJsonValue(src: string): string | null {
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
  return null;   // jamais refermé → c'est une troncature, balanceJson s'en charge
}

// Detect a ```ask block. Accepts {questions:[…]} and the legacy {question,options}.
// La fence de fermeture est optionnelle : si le modèle l'oublie, on prend tout
// ce qui suit plutôt que de ne rien reconnaître du tout.
function parseAsk(content: string): AskBlock | null {
  const m = content.match(/```ask\s*\n([\s\S]*?)(?:```|$)/);
  if (!m) return null;
  try {
    const body = m[1].trim();
    let obj;
    try {
      obj = JSON.parse(body);
    } catch {
      // 1) déchets après un objet complet → on coupe à la fermeture
      // 2) objet jamais refermé → on rééquilibre
      const repare = escapeRawControlChars(body);
      try {
        obj = JSON.parse(repare);
      } catch {
        obj = JSON.parse(firstJsonValue(repare) ?? balanceJson(repare));
      }
    }
    const raw: unknown[] = Array.isArray(obj.questions) ? obj.questions : (obj.question ? [obj] : []);
    const questions: AskQ[] = raw
      .map((q) => {
        const qq = q as { question?: unknown; options?: unknown };
        const question = typeof qq.question === "string" ? qq.question.trim() : "";
        const options = Array.isArray(qq.options)
          ? qq.options.filter((o: unknown) => typeof o === "string" && o.trim()).map((o: string) => o.trim()).slice(0, MAX_ASK_OPTIONS)
          : [];
        return { question, options };
      })
      .filter((q) => q.question && q.options.length >= 1)
      .slice(0, MAX_ASK_QUESTIONS);
    if (!questions.length) return null;
    return { questions, prose: content.replace(m[0], "").trim() };
  } catch {
    return null;
  }
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

// Client-side download of some text as a file (used for the Markdown export).
function downloadText(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// Extensions reconnues comme des FICHIERS. Liste fermée volontairement : sans
// elle, « ansible.builtin.reboot » ou « os_family['debian'] » passeraient pour
// des noms de fichiers et donneraient des titres absurdes.
const FILE_EXT = new RegExp(
  "\\.(ya?ml|json|jsonc|toml|ini|cfg|conf|env|py|js|mjs|cjs|ts|tsx|jsx|sh|bash|zsh|" +
  "go|rs|rb|php|java|kt|c|h|cpp|sql|html|css|scss|md|txt|log|xml|service|tf|gradle)$",
  "i");
const BARE_FILES = /^(Dockerfile|Makefile|Vagrantfile|Jenkinsfile|Procfile)$/i;

/** Nom de fichier annoncé juste AVANT un bloc de code.
 *
 * Un modèle écrit presque toujours « ### 2. `tasks/main.yml` » puis le bloc.
 * Sans lire ce contexte, les artefacts s'appelaient « file 1 », « yaml · 2 »… —
 * des cartes dont on ne pouvait plus dire à quel fichier elles correspondaient,
 * alors que la prose juste au-dessus, elle, nommait les fichiers.
 */
function titleFromContext(before: string): string {
  // Ne remonter que jusqu'au bloc PRÉCÉDENT : au-delà, on ramasse les
  // commentaires écrits À L'INTÉRIEUR du bloc d'avant (« # defaults/main.yml »)
  // et on nomme le fichier courant avec le nom du précédent.
  const finBlocPrecedent = before.lastIndexOf("```");
  const zone = finBlocPrecedent >= 0 ? before.slice(finBlocPrecedent + 3) : before;
  const tail = zone.slice(-300);
  const candidats: string[] = [];
  // 1) entre backticks — la forme la plus fiable
  for (const m of tail.matchAll(/`([^`\n]{1,80})`/g)) candidats.push(m[1].trim());
  // 2) sinon un jeton qui ressemble à un chemin
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

/** Le bloc de code encore ouvert à la fin du contenu, s'il y en a un.
 *
 * Pendant le flux, c'est le fichier que le modèle est en train d'écrire. On le
 * dirige vers le volet latéral au lieu de le laisser défiler dans le chat puis
 * de le déplacer d'un coup à la fin — ce qui donnait l'impression que les
 * fichiers étaient « recopiés » deux fois.
 */
function openCodeFence(content: string): { lang: string; body: string; start: number } | null {
  const fences = content.match(/```/g);
  if (!fences || fences.length % 2 === 0) return null;   // tout est refermé
  const start = content.lastIndexOf("```");
  const rest = content.slice(start + 3);
  const nl = rest.indexOf("\n");
  if (nl < 0) return null;                               // l'info-string n'est pas finie
  const info = rest.slice(0, nl).trim();
  const first = info.split(/\s+/)[0] || "";
  if (first === "ask") return null;                      // géré par parseAsk
  return { lang: first || "text", body: rest.slice(nl + 1), start };
}

function parseArtifacts(content: string, allowDoc: boolean): { prose: string; artifacts: Artifact[] } {
  const text = content.trim();
  // Un message qui contient un vrai bloc de code est un FICHIER, jamais un
  // document : le prendre pour un document renvoyait tout le message — question
  // comprise — sous un nom en .md. Ça dépendait de `allowDoc`, donc du message
  // PRÉCÉDENT, d'où un comportement qui changeait après un F5.
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
    if (first === "ask") continue; // handled by parseAsk, never a file artifact
    let lang = first;
    let title = "";
    if (first.includes(".")) { title = first; lang = first.split(".").pop() || ""; }
    const named = info.match(/(?:title|file|filename)=(\S+)/i);
    if (named) title = named[1];
    // Le nom annoncé dans la prose juste au-dessus vaut mieux qu'un numéro.
    if (!title) title = titleFromContext(content.slice(0, m.index));
    // Un bloc DONT LE NOM DE FICHIER EST ANNONCÉ est un fichier, même court : sinon
    // un rôle Ansible sortait avec deux fichiers en cartes et le troisième — plus
    // court — resté dans le chat, pour la même chose. Un extrait anonyme et court
    // reste en revanche dans le fil : c'est une illustration, pas un livrable.
    const substantial = !!title || body.length >= 200 || body.split("\n").length >= 6;
    if (!substantial) continue;
    n += 1;
    // Nom de repli : un vrai nom de fichier, pas une étiquette. « html · 1 » se
    // téléchargeait en « html · 1.txt » — ni lisible, ni ouvrable.
    if (!title) {
      const info = LANG_INFO[(lang || "").toLowerCase()];
      // Une page HTML seule s'appelle index.html — c'est ce qu'on attend d'elle,
      // et c'est ouvrable tel quel. Les autres gardent un nom neutre numéroté.
      if (info?.ext === "html" && n === 0) title = "index.html";
      else title = info ? `fichier-${n + 1}.${info.ext}` : `fichier-${n + 1}.txt`;
    }
    artifacts.push({ kind: "code", title, lang: lang || "text", content: body });
    prose += content.slice(lastIndex, m.index);
    lastIndex = fence.lastIndex;
  }
  prose += content.slice(lastIndex);

  // Filet : un document HTML écrit SANS bloc de code. Le modèle oublie
  // régulièrement la clôture pour un gros fichier — il n'en sortait alors aucun
  // fichier, donc pas de carte, pas d'aperçu, pas de téléchargement. L'utilisateur
  // se rabattait sur l'export de la conversation (bouton depuis retiré, il prêtait
  // justement à confusion) : d'où un .md contenant la question et la prose.
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

// Extension et type de contenu par langage. Sans extension, le navigateur ajoute
// « .txt » d'après le type MIME : un fichier nommé « html · 1 » se téléchargeait
// en « html · 1.txt », illisible et non ouvrable.
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

/** Nom réellement téléchargeable : ni espace ni « · », et toujours une extension
 *  cohérente avec le langage. */
function nomTelechargeable(titre: string, lang: string): string {
  if (/\.[a-z0-9]{1,6}$/i.test(titre)) return titre;
  const info = LANG_INFO[(lang || "").toLowerCase()];
  const base = titre.replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "fichier";
  return info ? `${base}.${info.ext}` : `${base}.txt`;
}

function mimePourLangage(lang: string): string {
  return LANG_INFO[(lang || "").toLowerCase()]?.mime ?? "text/plain";
}

/** Ce bloc est-il un extrait d'un fichier déjà connu, plutôt qu'une version ? */
function estFragment(
  ancien: { content: string; lang: string } | undefined,
  nouveau: string,
): boolean {
  if (!ancien) return false;
  // Seuils volontairement prudents : on ne refuse une mise à jour que si le
  // fichier connu est déjà conséquent ET que le nouveau bloc fait moins des
  // deux tiers. Une vraie réécriture qui raccourcit un peu passe donc encore.
  return ancien.content.length > 800 && nouveau.length < ancien.content.length * 0.66;
}

// Titre attribué faute de mieux (« fichier-2.html ») : le modèle n'a pas nommé
// son bloc.
const TITRE_GENERIQUE = /^fichier-\d+\.[a-z0-9]{1,6}$/i;

/** Les fichiers pour lesquels CE message ne contient qu'un extrait.
 *
 * Deux formes, vues toutes les deux en vrai :
 *  - le bloc porte le nom du fichier mais est bien plus court → extrait nommé ;
 *  - le bloc n'est pas nommé du tout (« voici la partie corrigée ») alors qu'un
 *    fichier bien plus gros du même langage existe déjà → extrait anonyme.
 */
function fragmentsDuMessage(messages: ChatMsg[], index: number): string[] {
  const m = messages[index];
  if (!m || m.role !== "assistant") return [];
  const avant = fichiersJusqua(messages, index - 1);
  const noms: string[] = [];
  for (const a of parseArtifacts(m.content, false).artifacts) {
    if (a.kind !== "code") continue;
    if (estFragment(avant.get(a.title), a.content)) { noms.push(a.title); continue; }
    if (!TITRE_GENERIQUE.test(a.title)) continue;
    // Bloc anonyme : à quel fichier connu du même langage pourrait-il appartenir ?
    const candidat = [...avant.entries()]
      .filter(([, f]) => f.lang === a.lang)
      .find(([, f]) => estFragment(f, a.content));
    if (candidat) noms.push(candidat[0]);
  }
  if (noms.length) return noms;

  // Dernier cas, le plus fréquent : un bloc COURT et sans nom (« voici la partie
  // corrigée »). Trop petit pour devenir un fichier, il reste dans le fil — mais
  // l'utilisateur, lui, attendait son fichier corrigé. On regarde donc les blocs
  // bruts, pas seulement ceux promus en fichiers.
  if (!avant.size) return [];
  for (const f of m.content.matchAll(/```([^\n`]*)\n([\s\S]*?)```/g)) {
    const lang = (f[1].trim().split(/\s+/)[0] || "").toLowerCase();
    if (lang === "ask" || lang === "edit") continue;
    const corps = f[2];
    const candidat = [...avant.entries()].find(
      ([, fic]) => (!lang || fic.lang.toLowerCase() === lang) && estFragment(fic, corps));
    if (candidat) return [candidat[0]];
  }
  return [];
}

/** État courant de chaque fichier de la conversation, jusqu'au message `index`.
 *
 * DÉRIVÉ du fil, jamais stocké : un fichier = sa dernière version complète, à
 * laquelle on applique dans l'ordre les modifications qui ont suivi. Rien à
 * synchroniser, donc rien qui puisse se désynchroniser — recharger la
 * conversation reconstruit exactement le même état.
 */
function fichiersJusqua(
  messages: ChatMsg[],
  index: number,
): Map<string, { content: string; lang: string }> {
  const fichiers = new Map<string, { content: string; lang: string }>();
  for (let i = 0; i <= index && i < messages.length; i++) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    for (const a of parseArtifacts(m.content, false).artifacts) {
      if (a.kind !== "code") continue;
      // Un bloc BIEN plus court qu'un fichier déjà connu du même nom est un
      // EXTRAIT (« voici la partie corrigée »), pas une nouvelle version. Le
      // prendre pour le fichier remplaçait 400 lignes par 20, et le volet
      // affichait ce moignon comme s'il était le fichier.
      if (estFragment(fichiers.get(a.title), a.content)) continue;
      fichiers.set(a.title, { content: a.content, lang: a.lang });
    }
    for (const e of parseEdits(m.content)) {
      // Le modèle peut nommer « index.html » un fichier enregistré sous
      // « site/index.html » : on retombe sur une correspondance par suffixe.
      const cle =
        (fichiers.has(e.file) && e.file) ||
        [...fichiers.keys()].find((k) => k === e.file || k.endsWith("/" + e.file) || e.file.endsWith("/" + k));
      if (!cle) continue;
      const cible = fichiers.get(cle)!;
      if (!cible.content.includes(e.find)) continue;   // ancre introuvable : on n'invente rien
      fichiers.set(cle, { ...cible, content: cible.content.replace(e.find, e.replace) });
    }
  }
  return fichiers;
}

/** Les modifications d'un message, avec le résultat et les échecs éventuels. */
function appliquerEdits(messages: ChatMsg[], index: number): {
  fichiers: Artifact[];
  echecs: string[];
} {
  const edits = parseEdits(messages[index]?.content ?? "");
  if (!edits.length) return { fichiers: [], echecs: [] };
  const avant = fichiersJusqua(messages, index - 1);
  const apres = fichiersJusqua(messages, index);
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

function estimateTokens(
  settings: Settings,
  input: string,
  messages: ChatMsg[],
  attachments: Attachment[],
): number {
  let chars = settings.system.length + input.length;
  for (const m of messages) chars += m.content.length;
  for (const a of attachments) chars += a.content.length;
  return Math.round(chars / 4);
}

type QueuedMsg = { content: string; text: string; attachmentCount?: number; ts: number };

export default function PlaygroundPage() {
  const t = useT();
  const { open: openSettings } = useSettingsDialog();
  const [csrf, setCsrf] = useState("");
  const [runningModels, setRunningModels] = useState<string[]>([]);
  const [modelLimits, setModelLimits] = useState<Record<string, number>>({});
  // null = pas encore su. On n'affiche l'alerte qu'une fois la réponse reçue,
  // pour ne pas faire clignoter un avertissement au chargement.
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);

  // File d'attente : messages soumis pendant qu'une réponse se génère. Au lieu
  // d'être perdus (l'ancien « if (streaming) return »), ils s'empilent dans un
  // panneau au-dessus du compositeur et partent TOUT SEULS dès que la réponse en
  // cours se termine. Les boutons de chaque ligne ne servent qu'à court-circuiter
  // cette attente : « Envoyer » interrompt la réponse en cours (sa partie déjà
  // écrite est conservée, comme avec Stop) pour passer à ce message tout de suite.
  const [queued, setQueued] = useState<QueuedMsg[]>([]);
  const queuedRef = useRef<QueuedMsg[]>([]);
  // runStream clôt la conversation depuis sa propre closure : pour repartir de
  // la liste à jour (réponse partielle conservée, etc.) on lit par ref.
  const messagesRef = useRef(messages);
  useEffect(() => { messagesRef.current = messages; });

  // ── Auto-défilement du fil pendant la génération ───────────────────────────
  // ChatLayout suit déjà le bas, mais il décroche par intermittence : quand un
  // gros morceau de texte arrive d'un coup (un paragraphe entier re-rendu),
  // l'écart au bas dépasse son seuil en UNE frame, il en déduit que le lecteur a
  // remonté et cesse de suivre jusqu'à ce qu'on redescende à la main. Mesuré :
  // sur deux exécutions identiques, une réponse suivait le bas sur 7/7
  // échantillons, l'autre décrochait sur 38/40.
  // On double donc son mécanisme par un suivi explicite, désarmé UNIQUEMENT
  // quand le lecteur remonte volontairement, et réarmé dès qu'il revient en bas.
  const suitLeBasRef = useRef(true);
  useEffect(() => {
    const el = document.querySelector<HTMLElement>(".astryx-chat-layout");
    if (!el) return;
    let precedent = el.scrollTop;
    const onScroll = () => {
      const ecart = el.scrollHeight - el.scrollTop - el.clientHeight;
      // Remontée d'au moins 4 px : c'est le lecteur, pas nous.
      if (el.scrollTop < precedent - 4) suitLeBasRef.current = false;
      if (ecart < 40) suitLeBasRef.current = true;
      precedent = el.scrollTop;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  // Après chaque rendu : si on suivait, on recolle au bas. Écriture DOM
  // uniquement — aucun état mis à jour, donc aucun rendu en cascade.
  useEffect(() => {
    if (!streaming || !suitLeBasRef.current) return;
    const el = document.querySelector<HTMLElement>(".astryx-chat-layout");
    if (el) el.scrollTop = el.scrollHeight;
  });

  const updateQueue = (q: QueuedMsg[]) => {
    queuedRef.current = q;
    setQueued(q);
  };
  // Le panneau de file fait grandir/rétrécir le dock, qui est sticky EN BAS du
  // scroller : sa hauteur s'ajoute donc au contenu, et si on suivait le bas, la
  // dernière ligne se retrouve masquée dessous. Astryx n'observe que le contenu
  // des messages, pas le dock — d'où ce recollage explicite. On ne le fait que
  // si on était déjà collé au bas, pour ne pas arracher quelqu'un qui relit
  // plus haut. Le scroller est le ChatLayout lui-même (pas de scrollRef fourni),
  // et la page n'en contient qu'un.
  useEffect(() => {
    const el = document.querySelector<HTMLElement>(".astryx-chat-layout");
    if (!el) return;
    // Marge large : l'écart vient d'AUGMENTER de la hauteur du panneau.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [queued.length]);
  // Débit en direct : `usage.completion_tokens` n'arrive qu'à la FIN du flux. On
  // estime donc le nombre de tokens à partir des CARACTÈRES reçus — compter les
  // deltas SSE serait faux (mesuré : avec le décodage spéculatif MTP, vLLM envoie
  // plusieurs tokens par delta, d'où ~2,7x de sous-estimation). Le ratio
  // caractères/token est auto-calibré à la fin de chaque génération sur le
  // `usage` exact, donc il s'adapte au modèle et à la langue (mesuré : ~4,5 en
  // français, ~5,0 en anglais). Refs : mises à jour à chaque delta sans re-render.
  const liveCharsRef = useRef(0);
  const charsPerTokenRef = useRef(4.8);
  const liveStartRef = useRef<number | null>(null);
  const [liveStats, setLiveStats] = useState<{ tokens: number; tps: number } | null>(null);

  // Rafraîchit le compteur affiché 4x/s pendant le flux — assez fluide à l'œil,
  // sans ajouter un re-render par token (updateLast en fait déjà un).
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
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [ctxUsed, setCtxUsed] = useState(0);
  const [firstName, setFirstName] = useState("");
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
  // Aperçu rendu d'une page HTML générée, plutôt que son code source.
  const [htmlPreview, setHtmlPreview] = useState(true);
  // URL de l'aperçu servi par le backend. Une iframe `srcdoc` hérite de la CSP
  // du portail et ses scripts inline sont bloqués : la page s'affiche mais rien
  // n'y répond. On passe donc par une réponse qui porte son propre bac à sable.
  const [previewUrl, setPreviewUrl] = useState("");
  // Plein écran du volet : indispensable pour regarder une page HTML générée,
  // illisible dans une colonne de 400 px.
  const [plein, setPlein] = useState(false);
  const liveDocOpenRef = useRef(false);
  /** Fermeture du panneau : sortir du plein écran ramène au volet latéral, on ne
   *  referme le fichier que si on n'y était pas. */
  const fermerPanneau = () => {
    if (plein) { setPlein(false); return; }
    setArtifact(null);
    setLiveDocOpen(false);
  };
  const openLiveDoc = () => { setLiveDocOpen(true); liveDocOpenRef.current = true; };

  const abortRef = useRef<AbortController | null>(null);

  // Dictation: same hook as the Voice and Video pages.
  const dictation = useDictation({ value: input, onChange: setInput, csrf });
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
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
    fetchCsrfToken().then(setCsrf).catch(() => {});
    fetchPlaygroundData()
      .then((data) => {
        setRunningModels(data.running_models);
        setModelLimits(data.model_limits);
        setHasKey(data.has_key);
        if (data.running_models.length) setModel(data.running_models[0]);
      })
      .catch(() => {});
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setFirstName(d?.fullname?.split(" ")[0] || ""))
      .catch(() => {});
  }, []);

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
      // `hidden` fait partie du message : sans lui, une réponse à des questions
      // redevenait un message ordinaire au rechargement, décalant les index et
      // changeant le rendu de la conversation.
      messages: msgs.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })),
    };
    // Optimistic on the UI side, then server save in the background: the
    // list must not wait for the network round-trip to update.
    setConversations((prev) => {
      const rest = prev.filter((c) => c.id !== item.id);
      return [item, ...rest];
    });
    if (csrf) void persistConversation(csrf, item);
    return item.id;
  }

  // Close any open document/file panel — it belongs to the conversation we're
  // leaving, not the one we're opening.
  function closeArtifact() {
    setArtifact(null);
    setLiveDocOpen(false);
  }

  function newConversation() {
    // Pas de ré-enregistrement ici : chaque génération sauvegarde déjà à sa fin.
    // Ré-enregistrer remontait la conversation en tête de liste au simple fait
    // d'en changer, alors que l'ordre doit refléter la dernière ACTIVITÉ.
    setMessages([]);
    setCurrentId(null);
    setCtxUsed(0);
    updateQueue([]);
    closeArtifact();
  }

  function selectConversation(conv: Conversation) {
    // Idem : ouvrir une conversation ne la modifie pas, donc ne doit pas la
    // faire remonter ni réécrire celle qu'on quitte.
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
    setCurrentId(conv.id);
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
    updateQueue([]);
    closeArtifact();
  }

  function deleteConversation(id: string) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (csrf) void removeConversation(csrf, id);
    if (id === currentId) setCurrentId(null);
  }

  function handleFiles(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        window.alert(`« ${file.name} » dépasse 96 Ko — trop gros pour le contexte.`);
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        setAttachments((prev) => [...prev, { name: file.name, content: String(reader.result) }]);
      };
      reader.readAsText(file);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function runStream(nextMessages: ChatMsg[]) {
    if (!model) {
      setMessages([...nextMessages, { role: "assistant", content: t("Aucun modèle actif.") }]);
      return;
    }
    setStreaming(true);
    // Nouvel envoi : le lecteur veut voir la réponse arriver, on réarme le suivi.
    suitLeBasRef.current = true;
    setLiveDocOpen(false);
    liveDocOpenRef.current = false;
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
    let usage: { total_tokens?: number; completion_tokens?: number } | undefined;

    const updateLast = () => {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", content: acc, reasoning: reason, ts: copy[copy.length - 1]?.ts };
        return copy;
      });
    };

    let isError = false;
    let tronque = false;
    let wasAborted = false;
    try {
      // On retire la capacité « poser des questions » UNIQUEMENT sur le tour qui
      // suit immédiatement des réponses — là où le modèle serait tenté d'enchaîner
      // question sur question au lieu de répondre.
      // Avant, ce test balayait TOUTE la conversation (`some`) : dès qu'on avait
      // répondu une fois, le modèle ne pouvait plus jamais demander de précisions,
      // même beaucoup plus tard sur une demande sans rapport. D'où l'impression
      // qu'il « fonçait » alors qu'il posait encore des questions en début de
      // conversation (mesuré : 16 demandes floues sur 18 donnent lieu à une
      // question, et 0 sur 9 demandes précises).
      const dernier = nextMessages[nextMessages.length - 1];
      const alreadyAsked = !!dernier?.hidden;
      const plafondModele = modelLimits[model];
      const askSettings = {
        ...settings,
        // Le plafond de sortie ne peut pas dépasser la fenêtre du modèle chargé.
        // Le backend le rabaisse déjà à ce qui reste ; on évite ici d'envoyer une
        // valeur qui n'a de sens pour aucun modèle en cours.
        maxTokens: plafondModele ? Math.min(settings.maxTokens, plafondModele) : settings.maxTokens,
        // L'ordre et le VOLUME comptent : mesuré sur 12 demandes floues, le modèle
        // pose une question 11 fois sur 12 avec la seule instruction de questions,
        // 7 fois sur 12 en y ajoutant nommage et édition, et 3 fois sur 12 si
        // l'instruction de questions passe en dernier. On garde donc les questions
        // EN TÊTE, un nommage réduit à une phrase, et l'instruction d'édition
        // seulement quand un fichier existe déjà — avant, elle ne sert à rien.
        system: [
          settings.system.trim(),
          alreadyAsked ? "" : ASK_INSTRUCTION,
          NAME_INSTRUCTION,
          fichiersJusqua(nextMessages, nextMessages.length - 1).size ? EDIT_INSTRUCTION : "",
        ].filter(Boolean).join("\n\n"),
      };
      await streamChat(
        csrf,
        model,
        nextMessages.map((m) => ({ role: m.role, content: m.content })),
        askSettings,
        controller.signal,
        (delta) => {
          if (delta.usage) usage = delta.usage;
          if (delta.truncated) tronque = true;
          if (delta.reasoningChunk) {
            if (tf === null) tf = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.reasoningChunk.length;
            reason += delta.reasoningChunk;
            updateLast();
          }
          if (delta.contentChunk) {
            if (tf === null) tf = performance.now();
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
      // Auto-calibrage : le vrai nombre de tokens est connu ici, on en déduit le
      // ratio caractères/token réel de ce modèle/cette langue pour que l'estimation
      // en direct de la PROCHAINE génération soit juste. Borné pour qu'une réponse
      // dégénérée (1 token, 500 caractères) ne fausse pas durablement l'affichage.
      const producedChars = acc.length + reason.length;
      if (tokens && producedChars > 0) {
        charsPerTokenRef.current = Math.min(12, Math.max(1.5, producedChars / tokens));
      }
      finalMessages.push({
        role: "assistant",
        content: trimAfterAsk(acc),
        truncated: tronque,
        reasoning: reason,
        tokens,
        tokensPerSec: tokens && gen > 0 ? Number((tokens / gen).toFixed(1)) : undefined,
        ttft: tf ? Number(((tf - t0) / 1000).toFixed(2)) : undefined,
        ts: startTs,
        isError,
      });
    }
    setMessages(finalMessages);
    // If the assistant wrote a file, or wrote a document in reply to a document
    // task, surface the last one in the side panel automatically.
    const lastUser = [...nextMessages].reverse().find((mm) => mm.role === "user");
    // A clarifying question is never a document/file artifact — leave the panel closed.
    const produced = parseAsk(acc) ? [] : parseArtifacts(acc, isDocTask(lastUser?.content ?? "")).artifacts;
    if (produced.length) {
      const lastArt = produced[produced.length - 1];
      // A code file opens on its own; a document opens automatically only if the
      // user was already watching it being written live (otherwise it stays a
      // card in the chat that they can click open).
      if (lastArt.kind === "code" || liveDocOpenRef.current) setArtifact(lastArt);
    }
    liveDocOpenRef.current = false;
    if (usage?.total_tokens) setCtxUsed(usage.total_tokens);
    const savedId = persist(finalMessages, currentId, model);
    setCurrentId(savedId ?? null);
    setStreaming(false);
    setLiveStats(null);
    abortRef.current = null;
    // Des messages ont été mis en file pendant cette génération : ils partent
    // maintenant, sans validation manuelle. Base explicite (finalMessages) :
    // messagesRef n'est pas encore resynchronisé dans cette même tick.
    if (queuedRef.current.length) dispatchQueued(finalMessages);
  }

  function send(value: string) {
    const text = value.trim();
    if (!text && !attachments.length) return;
    // La clé a pu être créée depuis la boîte de réglages entre-temps : on
    // revérifie ici plutôt que de laisser le bandeau (et l'échec) persister.
    // Une requête de plus, et uniquement dans l'état cassé.
    if (hasKey === false) {
      void fetchPlaygroundData().then((d) => setHasKey(d.has_key)).catch(() => {});
    }
    // Sending ends dictation: the message goes out with what has been
    // transcribed so far, and no still-in-flight pass will rewrite the
    // field once it's been cleared.
    dictation.cancel();
    let full = text;
    if (attachments.length) {
      full =
        (text ? text + "\n\n" : "") +
        attachments.map((f) => "```" + f.name + "\n" + f.content + "\n```").join("\n\n");
    }
    const attachmentCount = attachments.length || undefined;
    // En pleine génération, le message passe en file d'attente (validé par le
    // bouton Envoyer de sa bulle) au lieu d'être silencieusement perdu.
    if (streaming) {
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      updateQueue([...queuedRef.current, { content: full, text, attachmentCount, ts: Date.now() }]);
      setInput("");
      setAttachments([]);
      return;
    }
    const nextMessages: ChatMsg[] = [
      ...messages,
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      { role: "user", content: full, ts: Date.now(), attachmentCount },
    ];
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
    void runStream(nextMessages);
  }

  // « Envoyer » sur une ligne : ne pas attendre la fin de la réponse en cours.
  // On remonte ce message en tête de file puis on interrompt la génération — son
  // début est conservé, exactement comme avec Stop. L'envoi lui-même est fait par
  // la fin de runStream (l'abort est asynchrone : lire l'état ici perdrait la
  // réponse partielle).
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

  // « Modifier » : le message ressort de la file et retourne dans le compositeur.
  // On y remet `content` (et pas le texte brut) pour ne pas perdre en silence les
  // fichiers joints qui y ont été inlinés.
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
    const msgs: ChatMsg[] = q.map((m) => ({ role: "user", content: m.content, ts: m.ts, attachmentCount: m.attachmentCount }));
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

  /** Redemande le fichier ENTIER quand le modèle n'a renvoyé qu'un extrait. */
  function demanderFichierComplet(nom: string) {
    if (streaming) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Renvoie le fichier ${nom} EN ENTIER, du début à la fin, `
          + "avec la correction intégrée. Un seul bloc de code, aucun extrait, "
          + "aucune ligne omise, pas de « ... » ni de commentaire du type "
          + "« reste inchangé ».",
        // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Reprend une réponse coupée par le plafond de tokens, sans la refaire. */
  function continuer() {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: "Continue exactement là où tu t'es arrêté, sans rien répéter et "
          + "sans réintroduire ta réponse. Reprends au caractère suivant.",
        // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
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

  function editLast() {
    if (streaming || !messages.length) return;
    let base = messages;
    if (base[base.length - 1]?.role === "assistant") base = base.slice(0, -1);
    const last = base[base.length - 1];
    if (last?.role === "user") {
      setInput(last.content);
      setMessages(base.slice(0, -1));
    }
  }


  const max = modelLimits[model] || 32768;
  const used = Math.max(ctxUsed, estimateTokens(settings, input, messages, attachments));
  const lastMsg = messages[messages.length - 1];
  // While a document is being streamed, the chat shows a card (not the raw text)
  // and the side panel can show a live view of the document being written.
  const streamingDocActive =
    streaming && lastMsg?.role === "assistant" && isDocTask(messages[messages.length - 2]?.content ?? "");
  const liveContent = streamingDocActive ? (lastMsg?.content ?? "") : "";
  // Fichier de code en cours d'écriture : il s'affiche DIRECTEMENT dans le volet,
  // pas dans le chat. Sur écran étroit il n'y a pas de volet — on le laisse alors
  // défiler dans le chat, sinon l'utilisateur ne verrait rien s'écrire.
  const liveCode =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? openCodeFence(lastMsg.content ?? "")
      : null;
  const liveCodeTitle = liveCode
    ? titleFromContext((lastMsg?.content ?? "").slice(0, liveCode.start)) || `${liveCode.lang}`
    : "";
  const showLiveDoc = liveDocOpen && streamingDocActive;
  // Entre deux fichiers (bloc précédent refermé, suivant pas commencé), le volet
  // garde le dernier fichier terminé au lieu de se vider. Dérivé du contenu, sans
  // état : rien à synchroniser, donc rien à désynchroniser.
  const dernierFini =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? (parseArtifacts(lastMsg.content ?? "", false).artifacts.slice(-1)[0] ?? null)
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
      : (epingle?.title ?? "Document");
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
  // Une page HTML terminée peut être REGARDÉE, pas seulement lue en code. On ne
  // le propose pas tant qu'elle s'écrit : un rendu à moitié écrit clignote.
  const panelEstHtml = !liveCode && panelIsCode && /^html?$/i.test(panelLang);
  const panelDownloadName = panelIsCode
    ? nomTelechargeable(panelTitle, panelLang)
    : `${slugify(panelTitle)}.md`;
  const panelDownloadMime = panelIsCode ? mimePourLangage(panelLang) : "text/markdown";
  // Publie la page à prévisualiser dès que son contenu change. Écriture externe
  // (réseau) suivie d'une mise à jour d'état APRÈS l'await : pas de rendu en
  // cascade. Rien n'est publié tant qu'on ne regarde pas un aperçu.
  useEffect(() => {
    let annule = false;
    if (!panelEstHtml || !htmlPreview || !panelContent) {
      // Remise à zéro différée : mettre l'état à jour dans le CORPS de l'effet
      // déclencherait un rendu en cascade.
      void Promise.resolve().then(() => { if (!annule) setPreviewUrl(""); });
      return () => { annule = true; };
    }
    void sendJSON<{ ok: boolean; id?: string }>("/playground/preview", csrf, { html: panelContent })
      .then((r) => { if (!annule && r.ok && r.id) setPreviewUrl(`/playground/preview/${r.id}`); })
      .catch(() => {});
    return () => { annule = true; };
  }, [panelEstHtml, htmlPreview, panelContent, csrf]);
  // Auto-follow the document while it streams into the panel; show a "jump to
  // bottom" button when the reader scrolls up and leaves the live tail.
  const {
    setRef: panelScrollRefBrut,
    showButton: showPanelJump,
    onScroll: onPanelScroll,
    scrollToBottom: panelJumpDown,
  } = useStickToBottom(panelContent, showLive);
  // CodeBlock gère lui-même son défilement (dès qu'on lui donne un maxHeight) et
  // n'expose pas ce conteneur. Sans ça, un fichier long est ROGNÉ par un enfant
  // en overflow:hidden : plus rien ne déborde, donc plus rien ne défile et le
  // bouton « Descendre » n'apparaît jamais. Même remède que les logs de l'admin :
  // on pose la ref sur le parent et on descend chercher l'élément défilable.
  const panelScrollRef = useCallback(
    (node: HTMLElement | null) => {
      if (!node) return panelScrollRefBrut(null);
      const scroller =
        Array.from(node.querySelectorAll<HTMLElement>("*")).find((e) => {
          const o = getComputedStyle(e).overflowY;
          return o === "auto" || o === "scroll";
        }) ?? node;
      panelScrollRefBrut(scroller);
    },
    [panelScrollRefBrut],
  );
  const canRegenerate = !streaming && lastMsg && (lastMsg.role === "assistant" || lastMsg.role === "user");
  const canEdit = !streaming && messages.some((m) => m.role === "user");


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
                label={t("Réglages")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                isIconOnly
                onClick={() => setIsSettingsOpen((v) => !v)}
              />
            </HStack>
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent padding={0} isScrollable={false}>
          {isSettingsOpen && (
            <VStack padding={4}>
              <SettingsPanel settings={settings} onChange={setSettings} contexte={modelLimits[model]} />
            </VStack>
          )}
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
          <ChatLayout
            density="spacious"
            composer={
              <VStack gap={2} padding={4}>
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
                <ChatComposer
                  value={input}
                  onChange={setInput}
                  onSubmit={send}
                  isStopShown={streaming}
                  onStop={stop}
                  placeholder={t("Écris ton message… (Entrée pour envoyer, Maj+Entrée = saut de ligne)")}
                  input={<ChatComposerInput value={input} onChange={setInput} onSubmit={send} />}
                  headerActions={
                    <>
                      <Button
                        label={t("Joindre un fichier")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={PaperClipIcon} size="sm" />}
                        onClick={() => fileInputRef.current?.click()}
                      />
                      <DictateButton dictation={dictation} isDisabled={streaming} />
                    </>
                  }
                  headerContext={<ContextMeter used={used} max={max} />}
                  drawer={
                    attachments.length ? (
                      <ChatComposerDrawer count={attachments.length} label={t("Fichiers joints")}>
                        {attachments.map((f, i) => (
                          <Token
                            key={f.name + i}
                            label={`${f.name} (${Math.ceil(f.content.length / 1024)} Ko)`}
                            onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                          />
                        ))}
                      </ChatComposerDrawer>
                    ) : undefined
                  }
                  footerActions={
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
                />
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={ATTACH_ACCEPT}
                  style={{ display: "none" }}
                  onChange={(e) => handleFiles(e.target.files)}
                />
                <HStack hAlign="between" gap={2}>
                  <Text type="supporting" color="secondary">{t("Fichiers texte uniquement. Les tokens comptent sur ton budget.")}</Text>
                  <HStack gap={2}>
                    {canEdit && (
                      <Button
                        label={t("Éditer")}
                        variant="ghost"
                        size="sm"
                        icon={<Icon icon={PencilIcon} size="sm" />}
                        onClick={editLast}
                      />
                    )}
                    {canRegenerate && (
                      <Button
                        label={t("Régénérer")}
                        variant="ghost"
                        size="sm"
                        icon={<Icon icon={ArrowPathIcon} size="sm" />}
                        onClick={regenerate}
                      />
                    )}
                  </HStack>
                </HStack>
              </VStack>
            }>
            <ChatMessageList
              emptyState={
                <VStack gap={6} hAlign="center">
                  <VStack gap={1} hAlign="center">
                    <HStack gap={2} vAlign="center">
                      <Icon icon={SparklesIcon} size="md" color="accent" />
                      <Text type="large" as="h2">
                        {firstName ? `${t("Bonjour")}, ${firstName}` : t("Bonjour")}
                      </Text>
                    </HStack>
                    <Text type="display-2" as="h1">{t("Sur quoi veux-tu travailler ?")}</Text>
                  </VStack>
                  <Grid columns={{ minWidth: 220, max: 2 }} gap={3} width="100%">
                    {PRESETS.map((preset) => (
                      <ClickableCard
                        key={preset.heading}
                        label={t(preset.heading)}
                        variant="muted"
                        onClick={() => setInput(t(preset.prompt))}>
                        <VStack gap={1}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={preset.icon} size="sm" color="secondary" />
                            <Text weight="semibold">{t(preset.heading)}</Text>
                          </HStack>
                          <Text type="supporting" color="secondary">
                            {t(preset.body)}
                          </Text>
                        </VStack>
                      </ClickableCard>
                    ))}
                  </Grid>
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
                  // Les fichiers TERMINÉS deviennent des cartes tout de suite, sans
                  // attendre la fin de la réponse : parseArtifacts n'extrait que les
                  // blocs dont la fence de fermeture est arrivée, celui en cours reste
                  // donc de côté (volet) et non dans le chat. `allowDoc` reste réservé
                  // à la fin : basculer tout le message en document au milieu du flux
                  // ferait disparaître le texte déjà lu.
                  // Le fichier en cours d'écriture part dans le volet : on le
                  // retire une bonne fois du contenu analysé, pour qu'il ne
                  // ressorte ni en carte ni dans la prose du chat.
                  const contenuAffiche =
                    streamingThis && isLast && liveCode ? m.content.slice(0, liveCode.start) : m.content;
                  const arts = m.role === "assistant" && !ask
                    ? parseArtifacts(contenuAffiche, !streamingThis && isDocTask(messages[i - 1]?.content ?? ""))
                    : null;
                  // Un message peut ne contenir que des MODIFICATIONS : le fichier
                  // à montrer est alors le résultat, pas ce que le message contient.
                  const modifs = m.role === "assistant" && !streamingThis
                    ? appliquerEdits(messages, i)
                    : { fichiers: [], echecs: [] };
                  // Le modèle a renvoyé « la partie corrigée » au lieu du fichier.
                  const fragments = m.role === "assistant" && !streamingThis
                    ? fragmentsDuMessage(messages, i)
                    : [];
                  const items = [...(arts?.artifacts ?? []), ...modifs.fichiers];
                  // When the model puts everything in the artifact and writes nothing
                  // outside, still show a short line in the chat (not an empty bubble).
                  const emptyMsg = items.some((a) => a.kind === "doc")
                    ? t("Voici le document — ouvre-le pour le lire ou le copier.")
                    : t("Voici le fichier — ouvre-le pour le copier.");
                  // While streaming, hide a half-written ```ask block (raw JSON) —
                  // the question UI appears once the block is complete.
                  // Ce qui reste à afficher dans le chat pendant le flux : ni le
                  // bloc ```ask à moitié écrit (JSON brut), ni le fichier en cours
                  // d'écriture — ce dernier s'écrit dans le volet.
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
                  // Le bloc ```edit lui-même n'a rien à faire dans le chat : on
                  // garde la phrase d'explication, le résultat part en carte.
                  // Le bloc ```edit est TOUJOURS retiré du chat, même quand il n'a
                  // pas pu être appliqué : c'est du protocole, pas une réponse. En
                  // cas d'échec, le bandeau ci-dessous l'explique.
                  const contientEdit = m.content.includes("```edit");
                  const proseHorsEdit = (arts?.prose ?? m.content)
                    .replace(/```edit[\s\S]*?(?:```|$)/g, "")
                    .trim();
                  const bodyText = ask
                    ? askIntro
                    : items.length
                      ? (proseHorsEdit || emptyMsg)
                      : contientEdit && !streamingThis
                        ? (proseHorsEdit || t("Modification proposée."))
                        : streamingBody;
                  // Bloc présent mais rien appliqué et rien signalé : le parseur
                  // n'a pas pu le lire du tout. À dire, sinon la réponse paraît vide.
                  const editIllisible =
                    contientEdit && !streamingThis && !modifs.fichiers.length && !modifs.echecs.length;
                  // A document being streamed shows only a live-updating card in
                  // the chat (its raw text streams into the side panel instead).
                  const streamingDoc =
                    streamingThis && m.role === "assistant" && m.content.length > 0 &&
                    isDocTask(messages[i - 1]?.content ?? "");
                  return (
                  <ChatMessage key={i} sender={m.role}>
                    <ChatMessageBubble
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
                                      ? // Pendant le flux : compté sur les deltas SSE, donc « ~ ».
                                        `~${liveStats.tokens} tokens · ${liveStats.tps} tok/s`
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
                                    onClick={() => navigator.clipboard?.writeText(m.content)}
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
                                <Button
                                  label={t("Copier")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                  onClick={() => navigator.clipboard?.writeText(m.content)}
                                />
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {isThinking ? (
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
                          {m.reasoning ? (
                            <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                              <Markdown>{m.reasoning}</Markdown>
                            </Collapsible>
                          ) : null}
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
                          {m.reasoning ? (
                            <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                              <Markdown isStreaming={streamingThis}>{m.reasoning}</Markdown>
                            </Collapsible>
                          ) : null}
                          <Markdown isStreaming={streamingThis}>{bodyText || " "}</Markdown>
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
                          {/* Coupé par le plafond de tokens : sans ce message, la
                              réponse s'arrête en plein mot et rien ne l'explique. */}
                          {m.truncated && !streamingThis && (
                            <Banner
                              status="warning"
                              title={t("Réponse coupée")}
                              description={t(
                                "Le plafond de tokens a été atteint. Reprends la suite, ou augmente « Max tokens » dans les réglages.",
                              )}
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
                              label={a.kind === "code" ? `${t("Ouvrir le fichier")} ${a.title}` : t("Ouvrir le document")}
                              variant="muted"
                              onClick={() => setArtifact(a)}>
                              <HStack gap={2} vAlign="center">
                                <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                                <VStack gap={0}>
                                  <Text weight="semibold">{a.title}</Text>
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
                    <HStack gap={2} vAlign="center">
                      <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                      <VStack gap={0}>
                        <Text weight="semibold">{panelTitle}</Text>
                        {panelSubtitle ? <Text type="supporting" color="secondary">{panelSubtitle}</Text> : null}
                      </VStack>
                    </HStack>
                  }
                  endContent={
                    <>
                      <Button label={t("Plein écran")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ArrowsPointingOutIcon} size="sm" />}
                        onClick={() => setPlein(true)} />
                      <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                        onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                      <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                        onClick={() => navigator.clipboard?.writeText(panelContent)} />
                      <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={XMarkIcon} size="sm" />}
                        onClick={() => { setArtifact(null); setLiveDocOpen(false); }} />
                    </>
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
                {panelEstHtml && htmlPreview ? (
                  /* Page générée par le modèle, dans une iframe ISOLÉE.
                     `allow-scripts` SANS `allow-same-origin` : la page peut
                     s'exécuter — sinon boutons et interactions sont morts, ce qui
                     rend l'aperçu inutile pour une page interactive — mais son
                     origine reste OPAQUE. Elle ne peut donc ni lire les cookies
                     de session, ni toucher au DOM de la page qui l'héberge, ni
                     appeler l'API avec les droits de l'utilisateur.
                     `allow-same-origin` ne doit JAMAIS être ajouté ici : combiné à
                     `allow-scripts`, il annule le bac à sable et un HTML généré
                     deviendrait du code exécuté dans notre propre origine. */
                  <iframe
                    title={panelTitle}
                    src={previewUrl}
                    sandbox="allow-scripts allow-forms allow-modals allow-popups"
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
                  {panelIsCode
                    ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped maxHeight="100%" />
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
          <Dialog isOpen={historyOpen} onOpenChange={(o) => { if (!o) setHistoryOpen(false); }} width={560}>
            <DialogHeader
              title={t("Historique")}
              subtitle={`${conversations.length} ${t("conversations")}`}
              hasDivider
              onOpenChange={(o) => { if (!o) setHistoryOpen(false); }}
            />
            <VStack gap={1} padding={3} height={520} isScrollable>
              {conversations.length === 0 ? (
                <Text color="secondary">{t("Aucune conversation")}</Text>
              ) : (
                conversations.map((conv) => (
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
                      label={t("Supprimer")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={TrashIcon} size="sm" />}
                      onClick={() => deleteConversation(conv.id)}
                    />
                  </HStack>
                ))
              )}
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
                        <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                          onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                        <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                          onClick={() => navigator.clipboard?.writeText(panelContent)} />
                      </HStack>
                    }
                    startContent={
                      panelEstHtml ? (
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
                    /* Même aperçu isolé qu'en volet : le plein écran sert
                       justement à REGARDER la page, pas à relire son code. */
                    <iframe
                      title={panelTitle}
                      src={previewUrl}
                      sandbox="allow-scripts allow-forms allow-modals allow-popups"
                      style={{ width: "100%", height: "100%", border: "none",
                               background: "var(--color-background-surface)" }}
                    />
                  ) : (
                  <LayoutContent ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable>
                    {panelIsCode
                      ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped maxHeight="100%" />
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
