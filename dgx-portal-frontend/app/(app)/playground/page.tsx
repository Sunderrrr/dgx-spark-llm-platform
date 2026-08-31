"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Toolbar } from "@astryxdesign/core/Toolbar";
import { useResizable, ResizeHandle } from "@astryxdesign/core/Resizable";
import { Heading } from "@astryxdesign/core/Heading";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Selector } from "@astryxdesign/core/Selector";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Markdown, type MarkdownInlinePlugin } from "@astryxdesign/core/Markdown";
import katex from "katex";
import "katex/dist/katex.min.css";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Token } from "@astryxdesign/core/Token";
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
import { useT } from "@/lib/i18n";
import { useWhoami } from "@/lib/whoami";
import { useCsrf } from "@/lib/useCsrf";
import { useSettingsDialog } from "@/lib/settings-dialog";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { DictateButton } from "../_components/DictateButton";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { type EtapeWeb, fetchPlaygroundData, sendJSON, streamChat } from "@/lib/api";
import {
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
import { AskQuestion } from "./_components/AskQuestion";
import { ContextMeter, fmtK } from "./_components/ContextMeter";
import { SettingsPanel } from "./_components/SettingsPanel";
import { SkillsMenu } from "./_components/SkillsMenu";
import { SkillCreator } from "./_components/SkillCreator";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";
import { BASE_SKILLS, type Skill, loadCustomSkills, saveCustomSkills, skillMatches } from "@/lib/skills";

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

// Le protocole d'édition partielle a été retiré : sur du code généré qui contient
// des erreurs, une correction ponctuelle laissait un fichier à moitié juste, et
// une ancre mal recopiée ne s'appliquait pas du tout. On redemande le fichier
// ENTIER — c'est plus long à générer, mais ce qui sort est utilisable tel quel.
const REWRITE_INSTRUCTION = `When the user asks you to fix or change a file you already produced, output that file COMPLETE, from its first line to its last, under the exact same name. Never output a partial file, an excerpt, a diff, or a "rest unchanged" placeholder.`;

// Le modele ABANDONNE de lui-meme sur un gros fichier : mesure en prod le 22/08,
// il s'est arrete a 14 187 tokens sur 131 072 disponibles, avec
// finish_reason=stop (donc rien ne le distinguait d'une reponse reussie), en
// ecrivant « Le fichier est trop long pour etre affiche en entier ici » suivi
// d'un fichier tronque presente comme complet. Aucune limite technique n'etait
// atteinte. Cette instruction part a TOUS les tours, pas seulement quand un
// fichier existe deja, et passe EN DERNIER pour ne pas diluer ASK_INSTRUCTION
// (dont l'efficacite depend de sa position en tete, cf. mesure plus bas).
const INTEGRALITE_INSTRUCTION = `Never abridge a file you were asked to produce. Never write that a file is "too long to show here", never say you are giving a "shortened", "simplified" or "essential" version, and never replace any part of a file with an ellipsis, a placeholder, or a comment such as "rest of the code unchanged". There is no display limit: write the file in full, from its first line to its last. If you run out of room before the end, stop mid-file rather than closing it early — you will be asked to continue, and you will resume at the exact character where you stopped. A truncated file presented as complete is the worst possible answer.`;

// Placeholder du champ : on fait tourner quelques textes (dont l'astuce « / »
// pour appeler une compétence). Chaque entrée est une clé i18n (FR-as-msgid).
const PLACEHOLDER_TEXTS = [
  "Comment puis-je vous aider aujourd'hui ?",
  "Tapez / pour appeler une compétence",
  "Résumez un document, générez une image, écrivez du code…",
];

// Commandes « / » qui ouvrent le créateur de compétences.
const SLASH_CREATE_COMMANDS = ["skill-creator", "create", "new", "creer", "competence", "compétence"];

/* ── Compatibilité : anciennes conversations ────────────────────────────────
 * Le modèle ne reçoit plus le protocole d'édition (il réécrit le fichier en
 * entier). Ces fonctions restent parce que l'historique déjà enregistré
 * contient des blocs ```edit : sans elles, ces conversations réafficheraient
 * du JSON brut au lieu du fichier corrigé. Rien de neuf n'en produit.
 */

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

/** La réponse s'arrête-t-elle au milieu d'un fichier ?
 *
 * Le plafond de tokens n'est pas la seule façon de finir tronqué : un modèle de
 * cette taille lâche parfois prise au milieu d'un gros fichier et émet sa fin de
 * séquence en pleine expression. Le compteur dit alors « terminé » alors que le
 * fichier est inutilisable, et rien ne permettait de reprendre.
 */
function reponseIncomplete(content: string): boolean {
  const ouvert = openCodeFence(content);
  if (ouvert) {
    // Fence de fermeture simplement oubliée sur un fichier qui, lui, est fini :
    // ce n'est pas une coupure, et le dire relançait une génération pour rien.
    return !/<\/html\s*>\s*$/i.test(ouvert.body.trimEnd());
  }
  if (/<!DOCTYPE html|<html[\s>]/i.test(content) && !/<\/html\s*>/i.test(content)) return true;
  return false;
}

/** Le script de cette page HTML est-il refermé ?
 *
 * Constaté en production : le modèle écrit `</script></body></html>` alors qu'une
 * accolade reste ouverte au milieu. Le fichier a l'air terminé — il finit bien par
 * `</html>` — mais son JavaScript ne s'exécute pas du tout (« Unexpected end of
 * input »), plateau vide, page morte. Vérifier la balise de fin ne suffit donc pas.
 *
 * On ne peut PAS s'appuyer sur `new Function` pour le savoir : la CSP du portail
 * (`script-src 'self' 'nonce-…'`, sans `unsafe-eval`) l'interdit dans le navigateur,
 * et l'exception levée n'est alors même pas une SyntaxError. On compte donc les
 * blocs nous-mêmes, en sautant chaînes, gabarits, commentaires et littéraux
 * d'expression rationnelle — sans quoi la moindre accolade dans un texte fausserait
 * tout.
 */
function profondeurFinale(code: string): number {
  let i = 0, prof = 0;
  const n = code.length;
  // Pile des gabarits `...${ ... }...` : à l'intérieur d'un ${}, on relit du code.
  const gabarits: number[] = [];
  let precedent = "";                       // dernier caractère significatif
  const avantRegex = /[(,=:[!&|?{};+\-*%~^<>]/;
  const motsAvantRegex = /(?:^|[^\w$])(?:return|typeof|instanceof|in|of|new|delete|void|do|else|case|yield|await)$/;
  while (i < n) {
    const c = code[i];
    // — commentaires
    if (c === "/" && code[i + 1] === "/") { while (i < n && code[i] !== "\n") i++; continue; }
    if (c === "/" && code[i + 1] === "*") { i += 2; while (i < n && !(code[i] === "*" && code[i + 1] === "/")) i++; i += 2; continue; }
    // — chaînes
    if (c === "'" || c === '"') {
      const q = c; i++;
      while (i < n && code[i] !== q) { if (code[i] === "\\") i++; i++; }
      i++; precedent = "x"; continue;
    }
    // — gabarits
    if (c === "`") {
      i++;
      for (;;) {
        if (i >= n) return 1;               // gabarit jamais refermé → tronqué
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === "`") { i++; break; }
        if (code[i] === "$" && code[i + 1] === "{") { gabarits.push(prof); prof++; i += 2; break; }
        i++;
      }
      precedent = "x"; continue;
    }
    // — littéral d'expression rationnelle
    if (c === "/" && (precedent === "" || avantRegex.test(precedent)
                      || motsAvantRegex.test(code.slice(Math.max(0, i - 12), i)))) {
      i++;
      let classe = false;
      while (i < n) {
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === "[") classe = true;
        else if (code[i] === "]") classe = false;
        else if (code[i] === "/" && !classe) { i++; break; }
        else if (code[i] === "\n") break;   // pas une regex finalement
        i++;
      }
      precedent = "x"; continue;
    }
    if (c === "{" || c === "(" || c === "[") prof++;
    else if (c === "}" || c === ")" || c === "]") {
      prof--;
      // Une accolade qui referme un `${…}` fait retomber dans le gabarit.
      if (gabarits.length && prof === gabarits[gabarits.length - 1]) {
        gabarits.pop();
        i++;
        // on repart dans le gabarit jusqu'à son backtick
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
    if (/\bsrc\s*=/i.test(attrs)) continue;                       // script externe
    if (/type\s*=\s*["']?(?!text\/javascript|application\/javascript)[^"'\s>]+/i.test(attrs)) continue;
    const code = m[2];
    if (!code.trim()) continue;
    // Un bloc encore ouvert à la fin = fichier inutilisable. On ne signale QUE ce
    // sens-là : un excès de fermetures viendrait plus probablement d'une lecture
    // imparfaite de notre part que d'un vrai défaut.
    if (profondeurFinale(code) > 0) { casse = true; break; }
  }
  if (memoScript.size > 40) memoScript.clear();
  memoScript.set(contenu, casse);
  return casse;
}

/** Ce qu'affiche une étape de recherche web, en clair.
 *
 * Le modèle passe plusieurs dizaines de secondes à chercher et à lire avant de
 * répondre. Sans ce fil, l'attente est totalement muette et personne ne sait ce
 * qui se passe — c'est le premier retour d'usage qu'on a eu dessus.
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
    default:
      return { texte: outil, fini: true };
  }
}

/** Le modèle a-t-il annoncé quelque chose puis clos son tour ?
 *
 * Vu en production : « Bien sûr ! Quelques précisions pour bien t'aider : » — 13
 * tokens, puis fin normale du modèle (le journal serveur confirme un arrêt propre,
 * ni coupure réseau ni plafond). Le bloc de questions annoncé n'arrive jamais et
 * l'utilisateur se retrouve devant une phrase d'introduction toute seule.
 *
 * Une réponse ENTIÈRE qui se termine par deux-points, sans le moindre bloc de code,
 * n'est jamais une réponse finie : elle promet une suite qui n'est pas venue.
 */
function tourAvorte(m: ChatMsg | undefined): boolean {
  if (!m || m.role !== "assistant") return false;
  const t = m.content.trim();
  // Au-delà, c'est une vraie réponse qui se trouve finir par « : » (une liste
  // introduite, par exemple) — pas un tour avorté.
  if (!t || t.length > 400 || t.includes("```")) return false;
  return /[:：]$/.test(t);
}

/** Le texte du message, avec la fence jamais refermée refermée d'office.
 *
 * `parseArtifacts` n'extrait qu'un bloc DÉLIMITÉ des deux côtés. Une réponse
 * coupée en plein fichier n'en produisait donc aucun : ni carte, ni aperçu, ni
 * téléchargement — le code se déversait tel quel dans la bulle. On referme le
 * bloc pour récupérer ce qui a été écrit ; c'est toujours mieux que rien, et le
 * bandeau « Réponse coupée » dit que ce n'est pas fini.
 */
function contenuCloture(content: string): string {
  const fences = content.match(/```/g);
  return fences && fences.length % 2 === 1 ? content + "\n```" : content;
}

/** Le fichier que ce message laisse inachevé, s'il y en a un. */
function fichierInacheve(content: string): string | null {
  const fences = content.match(/```/g);
  if (!fences || fences.length % 2 === 0) return null;
  const arts = parseArtifacts(contenuCloture(content), false).artifacts;
  const dernier = arts[arts.length - 1];
  return dernier && dernier.kind === "code" ? dernier.title : null;
}

/** Ce qu'il faut recoller au fichier : le message de reprise, sans son emballage.
 *
 * Une reprise commence AU MILIEU du bloc. Trois formes vues en vrai :
 *  - le modèle rouvre une fence : on prend le corps du bloc ;
 *  - il enchaîne le contenu brut puis REFERME le bloc : le seul ``` est une
 *    fermeture, le corps est donc ce qui la PRÉCÈDE (le lire à l'envers ne
 *    rapportait que la phrase de conclusion, et le fichier restait tronqué) ;
 *  - il n'y a aucune fence : tout le message est du contenu.
 */
function corpsDeSuite(content: string): string {
  const ferme = content.match(/```[^\n`]*\n([\s\S]*?)```/);
  if (ferme) return ferme[1].replace(/\n$/, "");
  const premier = content.indexOf("```");
  if (premier < 0) return content;
  // Une fence d'OUVERTURE est suivie d'une info-string puis d'un saut de ligne, et
  // se trouve en tête du message ; sinon c'est une fermeture.
  const ouvrante = /^\s*```[^\n`]*\n/.test(content);
  if (ouvrante) return openCodeFence(content)?.body ?? content;
  return content.slice(0, premier).replace(/\n$/, "");
}

/** Recolle une suite sur un fichier inachevé, en absorbant ce qu'elle répète.
 *
 * Le modèle ne reprend pas au caractère près : il rafistole le mot coupé puis
 * réémet le bloc en cours depuis son début. Recoller bêtement dupliquait le code
 * et laissait une accolade en trop — la page s'affichait mais son script mourait
 * sur « Unexpected end of input », donc pas d'échiquier. On cherche la plus longue
 * suite de lignes commune entre la FIN du fichier et le DÉBUT de la reprise, et on
 * raboute là. La recherche est bornée à la queue du fichier : même trompée, elle ne
 * peut jamais amputer le début.
 */
const RECOL_QUEUE = 6000;      // portion de fin de fichier où l'on cherche
const RECOL_TETE  = 6000;      // portion de début de reprise où l'on cherche
const RECOL_MIN_LIGNES = 3;    // en dessous, c'est du bruit (« } », lignes vides)
const RECOL_MIN_CARS = 60;
const RECOL_MAX_SUPPRIME = 2000;   // au-delà, on préfère dupliquer que perdre

function decoupeLignes(texte: string, depart: number) {
  const lignes: { texte: string; pos: number }[] = [];
  let pos = depart;
  for (const l of texte.split("\n")) {
    lignes.push({ texte: l.trimEnd(), pos });
    pos += l.length + 1;
  }
  return lignes;
}

function recoller(base: string, suite: string): string {
  // Une suite qui réécrit VRAIMENT le document entier est un remplacement, pas un
  // ajout. On exige qu'elle aille jusqu'au bout et qu'elle pèse son poids : sans
  // ces deux conditions, une reprise de trois lignes commençant par « <html> »
  // effaçait un fichier de plusieurs milliers de lignes.
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
        if (t.length >= 10) utiles++;      // « } » ou ligne vide ne prouvent rien
        k++;
      }
      if (k >= RECOL_MIN_LIGNES && utiles >= 1 && cars >= RECOL_MIN_CARS
          && (!meilleur || cars > meilleur.cars)) {
        meilleur = { base: lb[i].pos, suite: ls[j].pos, cars };
      }
    }
  }
  // Garde-fou : le raboutage SUPPRIME la queue du fichier. Le chevauchement réel
  // observé est de quelques centaines de caractères (le modèle réémet le bloc en
  // cours) ; au-delà, une correspondance fortuite détruirait du bon code. Dans le
  // doute on recolle bout à bout : un peu de duplication se voit et se corrige,
  // du code disparu, non.
  if (meilleur && base.length - meilleur.base <= RECOL_MAX_SUPPRIME) {
    return base.slice(0, meilleur.base) + suite.slice(meilleur.suite);
  }
  return base + suite;
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

/** Reprises automatiques d'affilée avant de rendre la main à l'utilisateur. */
// Releve de 3 a 10 : un gros fichier unique demande une dizaine de segments
// (mesure : 2 400 a 14 000 tokens par reprise), et a 3 la chaine rendait
// toujours la main sur un fichier inacheve. Borne quand meme : une reprise
// qui boucle sans avancer doit revenir a l'utilisateur, pas tourner sans fin.
const MAX_REPRISES_AUTO = 10;

/** Ce message caché est-il la demande de reprise émise par « Continuer » ? */
function estReprise(m: ChatMsg | undefined): boolean {
  return !!m && m.role === "user" && !!m.hidden && m.content.startsWith(PROMPT_REPRISE);
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

// ── Export & partage d'une conversation ─────────────────────────────────────
// Une conversation à exporter : mêmes champs que l'API (même forme qu'ApiConversation
// côté lib/conversations).
type ExportConversation = {
  title: string;
  model: string;
  messages: { role: "user" | "assistant"; content: string; hidden?: boolean }[];
};

// Conversations épinglées (star) : préférence personnelle stockée côté navigateur —
// pas de migration backend, partagée sur ce poste.
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

// Math/LaTeX inline : rendu via KaTeX dans le Markdown. Le parseur Astryx n'a
// pas de nœud « math en bloc » — on couvre `$...$` et `\(...\)` (inline). Les
// blocs `$$...$$` restent du texte brut (limitation connue).
function renderMath(expr: string) {
  try {
    return katex.renderToString(expr, { throwOnError: false, output: "html", displayMode: false });
  } catch {
    return expr;
  }
}
const MATH_PLUGINS: MarkdownInlinePlugin[] = [
  // Convention KaTeX : `$...$` inline sans espace juste après `$` ni avant le
  // `$` fermant, pour ne pas confondre avec des montants (`$5 and $10`) ou du
  // texte mis entre dollars. KaTeX échoue sans lever d'erreur → retour au texte.
  {
    pattern: /\$(?!\s)([^$\n]+?)(?<!\s)\$/g,
    render: (m, key) => (
      <span key={key} className="cronos-inline-math" dangerouslySetInnerHTML={{ __html: renderMath(m[1]) }} />
    ),
  },
  {
    pattern: /\\\(([\s\S]+?)\\\)/g,
    render: (m, key) => (
      <span key={key} className="cronos-inline-math" dangerouslySetInnerHTML={{ __html: renderMath(m[1]) }} />
    ),
  },
];
// Snippets / prompts réutilisables, sauvegardés par l'utilisateur (navigateur).
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

// Onglets : plusieurs conversations ouvertes en parallèle. On garde la logique de
// génération intacte (les états live `messages`/`model`/… sont ceux de l'onglet
// ACTIF) ; on ne fait que sauvegarder/restaurer le snapshot de chaque onglet à la
// bascule. Le basculement est désactivé pendant un flux pour ne pas couper une
// réponse en cours.
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

// Renommage des fichiers générés (artefacts). Clé = `convId::kind::titre`
// (les noms de fichiers sont générés uniques par le modèle) et persistée en
// localStorage pour survivre à un rechargement ; `convId` évite qu'un même nom
// (`fichier-1.yml`) dans deux conversations se renomme l'un l'autre.
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

function convTitleFallback(msgs: ExportConversation["messages"], fallback: string): string {
  const first = msgs.find((m) => m.role === "user")?.content ?? "";
  return (first.slice(0, 80).trim() || fallback);
}

/** Conversation → Markdown lisible, exporté en .md. */
function convAsMarkdown(conv: ExportConversation): string {
  const lines = [`# ${conv.title}`, "", `_Modèle : ${conv.model || "—"}_`, "", "---", ""];
  for (const m of conv.messages) {
    if (m.hidden) continue;
    lines.push(m.role === "user" ? "**Vous :**" : "**Assistant :**");
    lines.push("", m.content, "");
  }
  return lines.join("\n");
}

/** Conversation → JSON complet (on conserve tout, y compris hidden), en .json. */
function convAsJson(conv: ExportConversation): string {
  return JSON.stringify(
    { title: conv.title, model: conv.model, exported_at: new Date().toISOString(), messages: conv.messages },
    null,
    2,
  );
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
    // ```ask et ```edit sont du PROTOCOLE, jamais des fichiers. `edit` ne posait pas
    // problème tant qu'un bloc non refermé n'était pas extrait ; depuis qu'on referme
    // les fences d'office, un bloc edit tronqué ressortait en « fichier-2.txt ».
    if (first === "ask" || first === "edit") continue;
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
      // `n` vient d'être incrémenté : le PREMIER bloc porte n === 1. Avec `n === 0`
      // la condition n'était jamais vraie et une page HTML sans nom annoncé
      // ressortait toujours en « fichier-2.html ».
      if (info?.ext === "html" && n === 1) title = "index.html";
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
  for (const a of parseArtifacts(contenuCloture(m.content), false).artifacts) {
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
  // Le fichier laissé en plan par le message précédent : une reprise le complète
  // au lieu d'ouvrir un second fichier avec la moitié du contenu.
  let inacheve: string | null = null;
  for (let i = 0; i <= index && i < messages.length; i++) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    // Une reprise ne se recolle que sur un fichier RÉELLEMENT laissé ouvert.
    // Se rabattre sur le dernier fichier connu paraissait prudent, mais recollait
    // la suite sur un fichier déjà terminé : deux </html>, accolades déséquilibrées.
    const cible = inacheve ? fichiers.get(inacheve) : undefined;
    // Un bloc de protocole n'est PAS la suite du fichier : le modèle a répondu à
    // « Continue » par un ```edit (constaté en production). Le recoller aurait
    // injecté du JSON au milieu du HTML — on le traite comme un message normal.
    const protocole = /```(?:edit|ask)\b/.test(m.content);
    if (estReprise(messages[i - 1]) && !protocole && !cible) {
      // Reprise sans rien à compléter (le fichier était déjà fini) : son contenu
      // n'est pas un fichier. En faire un donnait la carte « fichier-2.txt »
      // remplie d'un demi-script.
      inacheve = null;
      continue;
    }
    if (cible && !protocole && estReprise(messages[i - 1])) {
      const fusion: string = recoller(cible.content, corpsDeSuite(m.content));
      fichiers.set(inacheve!, { ...cible, content: fusion });
      // Une reprise peut être coupée à son tour. Son compte de fences ne dit rien
      // (elle commence au milieu d'un bloc) : c'est le fichier reconstitué qui
      // décide s'il reste ouvert.
      inacheve = reponseIncomplete(fusion) ? inacheve : null;
      continue;
    }
    inacheve = fichierInacheve(m.content);
    const arts = parseArtifacts(contenuCloture(m.content), false).artifacts;
    for (const [rang, a] of arts.entries()) {
      if (a.kind !== "code") continue;
      // Le bloc que la coupure a laissé ouvert est la version EN COURS d'écriture,
      // pas un extrait : sans ça, la reprise se recollait sur la version complète
      // précédente. On garde tout de même un plancher, pour qu'un moignon de
      // quelques lignes ne détruise pas un fichier abouti.
      const precedent = fichiers.get(a.title);
      if (rang === arts.length - 1 && a.title === inacheve
          && (!precedent || a.content.length >= precedent.content.length * 0.25)) {
        fichiers.set(a.title, { content: a.content, lang: a.lang });
        continue;
      }
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

/** Le fichier qu'un message de reprise vient de compléter.
 *
 * Ce message ne contient que la fin du fichier : ce qu'il faut montrer, c'est le
 * fichier entier reconstitué, pas la moitié qu'il transporte.
 */
function fusionDuMessage(messages: ChatMsg[], index: number): Artifact[] {
  const m = messages[index];
  if (!m || m.role !== "assistant" || !estReprise(messages[index - 1])) return [];
  const avant = fichiersJusqua(messages, index - 1);
  const apres = fichiersJusqua(messages, index);
  const out: Artifact[] = [];
  for (const [titre, f] of apres) {
    const a = avant.get(titre);
    if (a && a.content !== f.content) {
      out.push({ kind: "code", title: titre, lang: f.lang, content: f.content });
    }
  }
  return out;
}

/** Ce message laisse-t-il un fichier inachevé, reprises comprises ? */
function fichierLaisseOuvert(messages: ChatMsg[], index: number): boolean {
  const m = messages[index];
  if (!m || m.role !== "assistant") return false;
  // Une reprise commence AU MILIEU d'un bloc : elle n'a pas de fence ouvrante, donc
  // son compte de fences est toujours impair. S'y fier relançait une reprise après
  // l'autre alors que le fichier était refermé. Seul le fichier reconstitué décide.
  if (estReprise(messages[index - 1])) {
    const fusion = fusionDuMessage(messages, index);
    return fusion.length ? fusion.some((f) => reponseIncomplete(f.content)) : false;
  }
  return reponseIncomplete(m.content);
}

/* Le modèle qui AVOUE avoir abrégé.
 *
 * Cas vu en prod le 22/08 : « Le fichier est trop long pour être affiché en
 * entier ici », suivi d'un fichier tronqué. Un tel message peut parfaitement
 * refermer sa fence — le fichier a alors l'air fini et fichierLaisseOuvert() ne
 * voit rien, alors que le modèle vient de dire lui-même qu'il manque du contenu.
 *
 * Chaque motif est un AVEU explicite, jamais une tournure ordinaire : « version
 * simplifiée » ou « pour résumer » sont volontairement absents, ils apparaissent
 * dans des réponses parfaitement complètes et déclencheraient des reprises en
 * boucle (déjà vécu avec proseIncomplete, qui a détruit une conversation entière
 * en quatre reprises avant d'être retiré).
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
  // Seulement sur un message qui prétend livrer du code : la même phrase dans
  // une réponse en prose ne signale rien à reprendre.
  if (!content.includes("```")) return false;
  return ABANDON_DECLARE.test(content) || ABANDON_DECLARE_ALT.some((r) => r.test(content));
}

function messageIncomplet(messages: ChatMsg[], index: number): boolean {
  const m = messages[index];
  if (!m || m.role !== "assistant") return false;
  return fichierLaisseOuvert(messages, index) || abandonDeclare(m.content);
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
  const csrf = useCsrf();
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
  // Origine du prompt système : un persona, une compétence ou une saisie manuelle.
  // Permet de signaler qu'une compétence a écrasé le system prompt d'un persona.
  const [systemProvenance, setSystemProvenance] = useState<"persona" | "skill" | "manual">("manual");
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Recherche dans l'historique + conversations épinglées (star).
  const [histQuery, setHistQuery] = useState("");
  const [pinnedIds, setPinnedIds] = useState<string[]>(loadPinnedIds);
  // Renommage d'une conversation depuis l'historique.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [streaming, setStreaming] = useState(false);

  // Onglets : plusieurs conversations ouvertes. Le live state ci-dessus est
  // l'onglet actif ; chaque entrée de `tabs` en garde un snapshot.
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

  // File d'attente : messages soumis pendant qu'une réponse se génère. Au lieu
  // d'être perdus (l'ancien « if (streaming) return »), ils s'empilent dans un
  // panneau au-dessus du compositeur et partent TOUT SEULS dès que la réponse en
  // cours se termine. Les boutons de chaque ligne ne servent qu'à court-circuiter
  // cette attente : « Envoyer » interrompt la réponse en cours (sa partie déjà
  // écrite est conservée, comme avec Stop) pour passer à ce message tout de suite.
  // Reprises enchaînées sans intervention : au-delà, on rend la main plutôt
  // que de laisser un modèle qui boucle consommer le budget du compte.
  const reprisesRef = useRef(0);
  const [reprise, setReprise] = useState(0);
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
  // Étapes de recherche web de la génération EN COURS. Sans cet affichage,
  // l'attente est muette : plusieurs dizaines de secondes pendant lesquelles le
  // modèle cherche et lit, sans que rien ne l'indique.
  const [etapesWeb, setEtapesWeb] = useState<EtapeWeb[]>([]);
  // Première erreur d'exécution remontée par l'aperçu (vide = la page tourne).
  const [erreurApercu, setErreurApercu] = useState("");
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [ctxUsed, setCtxUsed] = useState(0);
  // Tokens cumulés de la conversation en cours (coût réel : chaque requête
  // facture prompt + complétion, donc la somme des `total_tokens` = ce qui est
  // débité du budget). Per-message, `m.tokens` est l'affichage déjà en place.
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
    fetchPlaygroundData()
      .then((data) => {
        setRunningModels(data.running_models);
        setModelLimits(data.model_limits);
        setHasKey(data.has_key);
        if (data.running_models.length) setModel(data.running_models[0]);
      })
      .catch(() => {});
  }, []);

  // Ouverture ciblée depuis l'accueil : ?conv=<id> ouvre une conversation
  // précise, ?model=<name> présélectionne le modèle courant. Appliqué une seule
  // fois, une fois les conversations ET les modèles courants chargés (les deux
  // fetchs sont dans des effets séparés, l'ordre n'est pas garanti).
  const initFromUrlRef = useRef(false);
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps -- init one-shot depuis l'URL */
  useEffect(() => {
    if (initFromUrlRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const convId = params.get("conv");
    const modelName = params.get("model");
    if (!convId && !modelName) return;
    if (runningModels.length === 0) return; // attente des modèles
    if (convId && conversations.length === 0) return; // attente des conversations
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
    // Avec les onglets, « nouvelle conversation » ouvre un onglet propre.
    newTab();
  }

  // Commute vers un onglet : on fige le snapshot de l'onglet actif puis on
  // restaure celui de la cible. Désactivé pendant un flux (l'état live est alors
  // celui de la génération en cours).
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
    setCtxUsed(0);
    setConvTokens(0);
    updateQueue([]);
    closeArtifact();
  }

  function closeTab(id: string) {
    if (streaming) return;
    const idx = tabsRef.current.findIndex((t) => t.id === id);
    if (idx < 0) return;
    // Titre généré à la fermeture : on analyse TOUTE la conversation qu'on
    // ferme (pas seulement son premier échange) pour poser un titre fiable
    // dans l'historique. Silencieux si la conversation n'a ni messages ni id.
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
      setCtxUsed(0);
      setConvTokens(0);
      updateQueue([]);
      closeArtifact();
    }
  }

  function selectConversation(conv: Conversation) {
    // Idem : ouvrir une conversation ne la modifie pas, donc ne doit pas la
    // faire remonter ni réécrire celle qu'on quitte.
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
    setCurrentId(conv.id);
    // L'onglet actif porte cette conversation (titre dans la barre d'onglets).
    setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, title: conv.title, currentId: conv.id } : t)));
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
    setConvTokens(0);
    updateQueue([]);
    closeArtifact();
  }

  function deleteConversation(id: string) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (csrf) void removeConversation(csrf, id);
    if (id === currentId) setCurrentId(null);
  }

  // Star/épingler une conversation (préférence navigateur, triée en tête).
  function togglePinned(id: string) {
    setPinnedIds((prev) => {
      const next = prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id];
      savePinnedIds(next);
      return next;
    });
  }

  // Snippets : sauvegarde le prompt courant, insère, supprime.
  function saveSnippet() {
    const content = input.trim();
    if (!content) return;
    // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
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

  // Auto-titre : demande au modèle un titre court, puis renomme la conversation.
  async function genTitle() {
    if (busyTitle || !messages.length || !currentId) return;
    setBusyTitle(true);
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model, messages });
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

  // Résumé : condense la conversation, affiché dans un dialog copiable.
  async function genSummary() {
    if (!messages.length) return;
    setSummary(t("Génération en cours…"));
    setSummaryOpen(true);
    try {
      const res = await sendJSON<{ summary?: string; error?: string }>("/api/playground/summarize", csrf, { model, messages });
      if (res.summary) setSummary(res.summary);
      else setSummary(res.error || t("Impossible de générer le résumé."));
    } catch {
      setSummary(t("Impossible de générer le résumé."));
    }
  }

  // Auto-titre déclenché à la première réponse : résumé court généré par le
  // modèle, propagé en direct à l'historique + l'onglet, sans rechargement.
  async function autoTitle(convId: string, msgs: ChatMsg[], tabId: string, modelForTitle?: string) {
    if (!csrf) return;
    const titleModel = modelForTitle || model;
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model: titleModel, messages: msgs });
      const title = res?.title;
      if (!title) return;
      setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, title } : c)));
      setTabs((prev) => prev.map((t) => (t.id === tabId || t.currentId === convId ? { ...t, title, currentId: convId } : t)));
      // eslint-disable-next-line react-hooks/purity -- handler async
      const item: Conversation = { id: convId, title, ts: Date.now(), model: titleModel, messages: msgs.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })) };
      void persistConversation(csrf, item);
    } catch {
      // silencieux : on garde le titre provisoire (début du prompt)
    }
  }

  // Renommage d'un fichier généré : applique le nouveau nom (persisté par
  // conversation) et ferme l'édition. Le titre se met à jour dans la carte du
  // chat, le volet et le nom de téléchargement.
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
  // Snippets / prompts réutilisables (bibliothèque navigateur).
  const [snippetsOpen, setSnippetsOpen] = useState(false);
  const [snippets, setSnippets] = useState<Snippet[]>(loadSnippets);
  // Compétences (skills) : compétences de base + celles créées par l'utilisateur.
  const [skills, setSkills] = useState<Skill[]>(() => [...BASE_SKILLS, ...loadCustomSkills()]);
  const [skillCreatorOpen, setSkillCreatorOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const customSkills = skills.filter((s) => !s.builtin);

  // Ouvre le créateur (édition si une compétence existante est fournie) et
  // referme le menu « / » pour ne pas superposer modal + menu.
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

  // Édition : on remplace la compétence du même id (les autres restent).
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

  // Sélection d'une compétence : on inscrit son prompt (localisé) dans le champ
  // et, si elle en a un, on applique son prompt système au modèle (en signalant
  // que la provenance devient « compétence »).
  function selectSkill(s: Skill) {
    setInput(t(s.prompt));
    const sp = s.systemPrompt;
    if (sp) {
      setSettings((prev) => ({ ...prev, system: sp }));
      setSystemProvenance("skill");
    }
  }

  // Auto-titre + résumé (générés par le modèle, facturés sur le budget).
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [busyTitle, setBusyTitle] = useState(false);
  // Panneau « contexte » : ce que le modèle voit (system, fichiers, tokens).
  const [ctxOpen, setCtxOpen] = useState(false);
  // Renommage des fichiers générés : map persistée (convId::kind::titre → nom).
  const [artifactRenames, setArtifactRenames] = useState<Record<string, string>>(loadArtifactRenames);
  const [renamingArtifact, setRenamingArtifact] = useState(false);
  const [renameArtifactValue, setRenameArtifactValue] = useState("");

  // Export (Markdown/JSON) de la conversation chargée.
  function exportConversation(conv: ExportConversation, fmt: "md" | "json") {
    const name = slugify(convTitleFallback(conv.messages, t("Conversation")));
    if (fmt === "json") downloadText(`${name}.json`, convAsJson(conv), "application/json");
    else downloadText(`${name}.md`, convAsMarkdown(conv), "text/markdown");
  }

  // Lien de partage en lecture seule : on crée l'instantané puis on copie l'URL.
  // Renommage d'une conversation (titre dérivé du 1er message par défaut).
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

  async function shareConversation(id: string) {
    if (!csrf) return;
    try {
      const res = await sendJSON<{ ok: boolean; token: string }>("/conversations/share", csrf, { client_id: id });
      if (res?.ok && res.token) {
        const url = `${window.location.origin}/c/${res.token}`;
        await navigator.clipboard.writeText(url);
        setShared(true);
        setTimeout(() => setShared(false), 2500);
      }
    } catch {
      /* lien non copié : silencieux */
    }
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
    setEtapesWeb([]);
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
    // Ce tour est-il une reprise (bouton « Continuer » ou reprise automatique) ?
    const enReprise = estReprise(dernier);
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
        // Sur un tour de REPRISE, ni questions ni protocole d'édition : le modèle
        // avait justement répondu par un bloc ```edit tronqué au lieu de finir le
        // fichier, ce qui laissait l'utilisateur sans rien.
        system: (enReprise
          ? [settings.system.trim(), REPRISE_INSTRUCTION]
          : [
              settings.system.trim(),
              alreadyAsked ? "" : ASK_INSTRUCTION,
              NAME_INSTRUCTION,
              fichiersJusqua(nextMessages, nextMessages.length - 1).size ? REWRITE_INSTRUCTION : "",
              INTEGRALITE_INSTRUCTION,
            ]
        ).filter(Boolean).join("\n\n"),
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
          if (delta.webStep) {
            const e = delta.webStep;
            // Une étape « finie » remplace son annonce, elle ne s'ajoute pas.
            setEtapesWeb((prec) => {
              const base = e.etape.endsWith("_finie") ? prec.slice(0, -1) : prec;
              return [...base, e].slice(-6);
            });
          }
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
    const produced = parseAsk(acc)
      ? []
      : parseArtifacts(contenuCloture(acc), isDocTask(lastUser?.content ?? "")).artifacts;
    if (produced.length) {
      const lastArt = produced[produced.length - 1];
      // A code file opens on its own; a document opens automatically only if the
      // user was already watching it being written live (otherwise it stays a
      // card in the chat that they can click open).
      if (lastArt.kind === "code" || liveDocOpenRef.current) setArtifact(lastArt);
    }
    liveDocOpenRef.current = false;
    const total = usage?.total_tokens;
    if (total) {
      setCtxUsed(total);
      setConvTokens((p) => p + total);
    }
    const savedId = persist(finalMessages, currentId, model);
    setCurrentId(savedId ?? null);
    // L'onglet actif porte immédiatement cette conversation (avant même que
    // l'auto-titre ne réponde), pour que fermer/commuter reste cohérent.
    setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, currentId: savedId ?? null } : t)));
    // (Auto-titre à la fermeture uniquement — on analyse la conversation complète
    // pour un titre d'historique fiable ; voir closeTab.)
    setStreaming(false);
    setLiveStats(null);
    abortRef.current = null;
    // Le modèle a lâché en plein fichier : on enchaîne tout seul. Constaté en
    // production — un gros fichier HTML s'arrêtait en pleine expression sans
    // que rien ne l'indique, et il fallait tout redemander. Le plafond de
    // tokens n'y était pour rien : c'est le modèle qui rend la main trop tôt.
    // `isError` n'exclut PAS la reprise : la coupure la plus fréquente est justement
    // une erreur — le flux casse en cours de route sur une génération de plusieurs
    // minutes, et le texte déjà reçu s'arrête en plein mot. C'est exactement le cas
    // où reprendre tout seul a le plus de valeur. Seul un arrêt DEMANDÉ (bouton
    // Stop) interdit la reprise.
    // Tour avorté (annonce sans la suite) : on REFAIT le tour au lieu de le
    // reprendre — il n'y a rien à prolonger, la réponse n'a jamais commencé.
    if (!wasAborted && tourAvorte(finalMessages[finalMessages.length - 1])) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        void runStream(nextMessages);
        return;
      }
    }
    if (!wasAborted && messageIncomplet(finalMessages, finalMessages.length - 1)) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        // Fichier laissé OUVERT : on le prolonge au caractère suivant. Fichier
        // REFERMÉ mais abrégé de l'aveu du modèle : le prolonger produirait du
        // contenu après la dernière ligne d'un fichier déjà clos — c'est une
        // réécriture complète qu'il faut demander.
        const suite = fichierLaisseOuvert(finalMessages, finalMessages.length - 1)
          ? PROMPT_REPRISE_COMPLET
          : PROMPT_INTEGRAL;
        void runStream([...finalMessages, {
          role: "user", content: suite,
          // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
          ts: Date.now(), hidden: true,
        }]);
        return;
      }
      // Trop de reprises d'affilée : le bandeau et le bouton prennent le relais.
    }
    reprisesRef.current = 0;
    setReprise(0);
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
        content: PROMPT_REPRISE_COMPLET,
        // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Le fichier livré ne s'exécute pas : on en redemande une version complète.
   *
   * Surtout PAS une reprise : la coupure n'est pas à la fin, elle est au milieu
   * (un bloc jamais refermé). Ajouter du texte à la suite n'y changerait rien.
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
        // eslint-disable-next-line react-hooks/purity -- appelé depuis un handler
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** L'aperçu a levé une erreur : on redemande le fichier corrigé, en entier. */
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

  // Éditer un message PASSÉ et rebrancher la suite : on garde tout ce qui
  // précède ce tour, on retire ce message + la suite, et on remet son contenu
  // dans l'input pour le renvoyer (la conversation repart de là).
  function editMessage(i: number) {
    if (streaming) return;
    const m = messages[i];
    if (!m || m.role !== "user") return;
    setMessages(messages.slice(0, i));
    setInput(m.content);
  }


  const max = modelLimits[model] || 32768;
  const used = Math.max(ctxUsed, estimateTokens(settings, input, messages, attachments));
  // Décomposition du contenu pour « Fenêtre de contexte » : prompt système vs
  // messages/fichiers (même estimation chars/4 que estimateTokens). Le backend
  // ne fournit pas le décompte par segment, on estime donc la part relative —
  // assez fidèle pour visualiser ce qui occupe la fenêtre.
  const systemTokens = Math.round(settings.system.length / 4);
  let contentChars = input.length;
  for (const m of messages) contentChars += m.content.length;
  for (const a of attachments) contentChars += a.content.length;
  const contentTokens = Math.round(contentChars / 4);
  const totalEstimate = Math.max(1, systemTokens + contentTokens);
  const sysPct = Math.round((systemTokens / totalEstimate) * 100);
  const msgPct = 100 - sysPct;
  const ctxLevel: "accent" | "warning" | "error" =
    used / (max || 1) >= 0.95 ? "error" : used / (max || 1) >= 0.8 ? "warning" : "accent";

  // Liste d'historique : recherche par titre + épinglées en tête, puis récentes.
  const q = histQuery.trim().toLowerCase();
  const visibleConvs = conversations
    .filter((c) => !q || (c.title || "").toLowerCase().includes(q))
    .sort((a, b) => {
      const pa = pinnedIds.includes(a.id) ? 0 : 1;
      const pb = pinnedIds.includes(b.id) ? 0 : 1;
      return pa - pb || ((b.ts ?? 0) - (a.ts ?? 0));
    });
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
      .then((r) => {
        if (annule || !r.ok || !r.id) return;
        setErreurApercu("");            // nouvelle page : on repart d'un état propre
        setPreviewUrl(`/playground/preview/${r.id}`);
      })
      .catch(() => {});
    return () => { annule = true; };
  }, [panelEstHtml, htmlPreview, panelContent, csrf]);

  // L'aperçu remonte ses erreurs d'exécution. Une page peut être parfaitement
  // formée et ne rien faire — mauvaise version de bibliothèque, méthode
  // inexistante — et aucune analyse du texte ne peut le voir. Seule l'exécution
  // le dit, et c'est l'aperçu qui exécute.
  useEffect(() => {
    function surMessage(e: MessageEvent) {
      const d = e.data as { cronosPreviewError?: unknown } | null;
      const msg = d && typeof d.cronosPreviewError === "string" ? d.cronosPreviewError : null;
      if (!msg) return;
      // Contenu produit par la page générée : jamais interprété, seulement affiché.
      setErreurApercu((prec) => prec || msg.slice(0, 300));
    }
    window.addEventListener("message", surMessage);
    return () => window.removeEventListener("message", surMessage);
  }, []);
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

  // Salutation horaire pour le premier message (type Claude). « soir » à partir de 18h.
  const playHour = new Date().getHours();
  const greeting =
    playHour >= 18 || playHour < 5
      ? t("Bonsoir, comment allez-vous ?")
      : t("Bonjour, comment allez-vous ?");
  // Premier message : on centre le composeur avec la salutation au-dessus.
  const isFirstEmpty = messages.length === 0 && !isSettingsOpen && !streaming;

  // Placeholder rotatif : quelques textes qui défilent (dont l'astuce « / »
  // pour appeler une compétence). Le timer est indépendant du rendu : il fait
  // simplement avancer l'index dans PLACEHOLDER_TEXTS.
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setPlaceholderIdx((i) => (i + 1) % PLACEHOLDER_TEXTS.length), 5000);
    return () => clearInterval(id);
  }, []);
  const placeholderText = t(PLACEHOLDER_TEXTS[placeholderIdx] ?? PLACEHOLDER_TEXTS[0]);

  // Mode « /compétence » : dès que le champ commence par un « / », on affiche
  // le menu des compétences. Le texte après « / » sert de filtre.
  const slashQuery = input.startsWith("/") ? input.slice(1).trim().toLowerCase() : null;

  // Commandes « / » spéciales : /skill-creator (ou /create, /new) ouvre le
  // créateur de compétence et vide le champ. Détecté à la saisie (onChange),
  // pas dans un effect, pour respecter la règle react-hooks/set-state-in-effect.
  function handleInput(v: string) {
    const cmd = v.startsWith("/") ? v.slice(1).trim().toLowerCase() : null;
    if (cmd && SLASH_CREATE_COMMANDS.includes(cmd)) {
      openSkillCreator();
      return;
    }
    setInput(v);
  }

  // Compétences filtrées (pour la navigation clavier + le menu) : base puis créées.
  const baseHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  const customHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => !s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  // Ligne 0 = carte « créer », lignes 1..n = compétences (base puis créées).
  const skillHits = useMemo(() => [...baseHits, ...customHits], [baseHits, customHits]);
  const menuRows = 1 + skillHits.length;

  // Ligne surlignée dans le menu (0 = « Créer un skill », 1..n = compétences).
  const [skillSel, setSkillSel] = useState(1);
  useEffect(() => {
    // On remonte le surlignage à la première compétence à chaque changement de
    // filtre (sync depuis l'état du champ, cas légitime autorisé par le lint).
    if (slashQuery !== null) {
      /* eslint-disable react-hooks/set-state-in-effect */
      setSkillSel(1);
      /* eslint-enable react-hooks/set-state-in-effect */
    }
  }, [slashQuery]);

  // Handler de navigation clavier, maintenu à jour à chaque rendu via une ref :
  // l'effet d'écoute ne dépend que de l'ouverture du menu, sans dépendances
  // instables (fonctions / tableaux recréés), donc 0 warning exhaustive-deps.
  const onMenuKeyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    onMenuKeyRef.current = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.min(s + 1, menuRows - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.max(s - 1, 0));
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setInput("");
      } else if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        const sel = Math.min(skillSel, menuRows - 1);
        if (sel === 0) openSkillCreator();
        else {
          const s = skillHits[sel - 1];
          if (s) selectSkill(s);
        }
      }
    };
  });

  useEffect(() => {
    if (slashQuery === null) return;
    function onMenuKey(e: KeyboardEvent) {
      onMenuKeyRef.current(e);
    }
    // Écoute en PHASE DE CAPTURE sur la fenêtre pour devancer le champ (sinon
    // ↑/↓ relit l'historique et Entrée envoie le message).
    window.addEventListener("keydown", onMenuKey, true);
    return () => window.removeEventListener("keydown", onMenuKey, true);
  }, [slashQuery]);
  const effectiveSel = Math.min(skillSel, menuRows - 1);

  // Nœud « composer » réutilisé à la fois dans le layout ancré en bas (conversation
  // en cours) et centré sur le premier message. Barre en bas : Attacher (gauche),
  // sélecteur de modèle + bouton micro/envoyer (droite).
  const composerNode = (
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
        onChange={handleInput}
        onSubmit={send}
        isStopShown={streaming}
        onStop={stop}
        placeholder={placeholderText}
        input={<ChatComposerInput value={input} onChange={handleInput} onSubmit={send} />}
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
          <>
            {messages.length > 0 && (
              <Button
                label={t("Fenêtre de contexte")}
                variant="ghost"
                size="sm"
                isIconOnly
                icon={<StatusDot variant={ctxLevel} label={t("Fenêtre de contexte")} />}
                onClick={() => setCtxOpen(true)}
              />
            )}
            <Button
              label={t("Joindre un fichier")}
              variant="ghost"
              size="sm"
              isIconOnly
              icon={<Icon icon={PaperClipIcon} size="sm" />}
              onClick={() => fileInputRef.current?.click()}
            />
          </>
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
              <Button
                label={t("Réglages")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                isIconOnly
                onClick={() => setIsSettingsOpen((v) => !v)}
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
          {isSettingsOpen && (
            <VStack padding={4}>
              <SettingsPanel
                settings={settings}
                onChange={setSettings}
                contexte={modelLimits[model]}
                provenance={systemProvenance}
                onProvenance={setSystemProvenance}
              />
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
          {/* Indicateur de contexte global, discret, en haut de la zone de chat.
              Uniquement quand une conversation est en cours (pas sur l'écran
              d'accueil centré, où le carré reste épuré). */}
          {!isFirstEmpty && (
            <HStack hAlign="end" padding={2}>
              <HStack width={300}>
                <ContextMeter used={used} max={max} />
              </HStack>
            </HStack>
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
            composer={composerNode}>
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
                    ? parseArtifacts(
                        // Fence jamais refermée : on la referme, sinon le fichier
                        // coupé reste du code brut au milieu de la bulle.
                        streamingThis ? contenuAffiche : contenuCloture(contenuAffiche),
                        !streamingThis && isDocTask(messages[i - 1]?.content ?? ""))
                    : null;
                  // Message de reprise : il ne porte que la fin du fichier.
                  const suite = m.role === "assistant" && !streamingThis
                    ? fusionDuMessage(messages, i)
                    : [];
                  // Un message peut ne contenir que des MODIFICATIONS : le fichier
                  // à montrer est alors le résultat, pas ce que le message contient.
                  const modifs = m.role === "assistant" && !streamingThis
                    ? appliquerEdits(messages, i)
                    : { fichiers: [], echecs: [] };
                  // Le modèle a renvoyé « la partie corrigée » au lieu du fichier.
                  const fragments = m.role === "assistant" && !streamingThis
                    ? fragmentsDuMessage(messages, i)
                    : [];
                  // Le flux a cassé APRÈS avoir livré du texte : ce n'est pas une
                  // heuristique, c'est une erreur constatée. Sans ce signalement, la
                  // réponse s'arrêtait en plein mot sans que rien ne l'explique.
                  const coupeReseau = !!m.isError && m.role === "assistant"
                    && m.content.length > 200 && !streamingThis;
                  // Un message de REPRISE ne montre que le fichier reconstitué. S'il
                  // n'y avait rien à compléter, il ne montre aucun fichier : son
                  // contenu est la suite d'un texte, pas un livrable. Sans ce test,
                  // la carte « fichier-2.txt » (un demi-script) revenait dans le fil.
                  const estSuite = m.role === "assistant" && !streamingThis
                    && estReprise(messages[i - 1]);
                  const items = estSuite
                    ? [...suite, ...modifs.fichiers]
                    : [...(arts?.artifacts ?? []), ...modifs.fichiers];
                  // Un fichier qui se termine par </html> mais dont le script ne compile
                  // pas est inutilisable : rien ne le signalait, la page restait vide.
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
                  // Une reprise n'a pas de prose à montrer : tout son contenu est
                  // la fin du fichier, déjà recollée dans la carte.
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
                                        // Une reprise automatique est dite : sinon la réponse
                                        // semble repartir de nulle part.
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
                                <HStack gap={1} vAlign="center">
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
                                    onClick={() => navigator.clipboard?.writeText(m.content)}
                                  />
                                </HStack>
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {/* Recherche web en cours : on dit ce qui est cherché et lu,
                          au fil de l'eau. Sans ça l'attente est muette pendant
                          des dizaines de secondes. */}
                      {streamingThis && etapesWeb.length > 0 && (
                        <VStack gap={1} padding={2}>
                          {etapesWeb.map((e, k) => {
                            const l = libelleEtapeWeb(e, t);
                            return (
                              <HStack key={`web-${k}`} gap={2} vAlign="center">
                                <StatusDot variant={l.fini ? "success" : "accent"}
                                  isPulsing={!l.fini} label={l.texte} />
                                <Text type="supporting" color="secondary">{l.texte}</Text>
                              </HStack>
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
                          {m.reasoning ? (
                            <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                              <Markdown>{m.reasoning}</Markdown>
                            </Collapsible>
                          ) : null}
                          {bodyText.trim() ? <Markdown inlinePlugins={MATH_PLUGINS}>{bodyText}</Markdown> : null}
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
                          <Markdown isStreaming={streamingThis} inlinePlugins={MATH_PLUGINS}>{bodyText || " "}</Markdown>
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
                          {(m.truncated || coupeReseau || (!streamingThis && messageIncomplet(messages, i))) && !streamingThis && (
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
                          onClick={() => navigator.clipboard?.writeText(panelContent)} />
                        <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={XMarkIcon} size="sm" />}
                          onClick={() => { setArtifact(null); setLiveDocOpen(false); }} />
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
                  /* La page s'ouvre mais lève une erreur : elle est bien formée et
                     pourtant inutilisable. Sans ce bandeau, l'utilisateur voit un
                     aperçu vide ou figé sans savoir pourquoi. */
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
                    : <Markdown isStreaming={showLive} inlinePlugins={MATH_PLUGINS}>{panelContent || " "}</Markdown>}
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
                        icon={<Icon icon={StarIcon} size="sm" />}
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
                  onClick={() => navigator.clipboard?.writeText(summary)}
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
                <ProgressBar label={t("Utilisation du contexte")} isLabelHidden value={used} max={max || 1} variant={ctxLevel} />
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
                <Badge label={`${Math.round((used / (max || 1)) * 100)} % ${t("contexte")}`} variant="info" />
                {convTokens > 0 && (
                  <Badge label={`${convTokens.toLocaleString("fr-FR")} ${t("tokens")}`} variant="info" />
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
                      <Text type="supporting" color="secondary">{Math.ceil(f.content.length / 1024)} Ko</Text>
                    </HStack>
                  ))
                )}
              </VStack>
              <Text type="supporting" color="secondary">
                {t("La fenêtre de contexte est partagée : system prompt + messages + fichiers. Le % indique ce qui est utilisé.")}
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
                              onClick={() => navigator.clipboard?.writeText(panelContent)} />
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
                      : <Markdown isStreaming={showLive} inlinePlugins={MATH_PLUGINS}>{panelContent || " "}</Markdown>}
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
