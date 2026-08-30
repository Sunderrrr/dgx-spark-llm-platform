// Compétences (skills) de type Claude : une compétence = une commande /alias qui
// prépare le model avec un prompt à envoyer (+ un prompt système optionnel).
// Les compétences de base sont intégrées ; les compétences créées par
// l'utilisateur vivent en localStorage (comme les snippets).

export type Skill = {
  id: string;
  /** Libellé affiché (msgid FR pour les compétences de base). */
  name: string;
  /** Commande /alias (ex. "resumer" -> taper /resumer). */
  alias: string;
  /** Sous-titre affiché dans le menu. */
  description: string;
  /** Texte inscrit dans le champ à la sélection. */
  prompt: string;
  /** Prompt système appliqué à la sélection (comportement du model). */
  systemPrompt?: string;
  builtin?: boolean;
};

export const SKILLS_KEY = "cronos.skills";

export const BASE_SKILLS: Skill[] = [
  {
    id: "resumer",
    name: "Résumer",
    alias: "resumer",
    description: "Condense un texte en points clés",
    prompt: "Résume ce texte en 3 points clairs et concis : ",
    systemPrompt:
      "You are a precise summarizer. Keep only the key points, in the language of the input, and stay concise.",
    builtin: true,
  },
  {
    id: "expliquer",
    name: "Expliquer",
    alias: "expliquer",
    description: "Décompose un sujet technique simplement",
    prompt: "Explique-moi ce sujet simplement, comme à un débutant : ",
    systemPrompt:
      "You explain clearly, with simple analogies and no jargon. Assume the reader is smart but new to the topic.",
    builtin: true,
  },
  {
    id: "coder",
    name: "Coder",
    alias: "coder",
    description: "Génère une fonction, un script ou un test",
    prompt: "Écris le code suivant, complet et exécutable : ",
    systemPrompt:
      "You are a senior software engineer. Produce complete, runnable, idiomatic code. Prefer whole files over snippets and never elide parts of a file.",
    builtin: true,
  },
  {
    id: "logs",
    name: "Analyser des logs",
    alias: "logs",
    description: "Trouve la cause d'une erreur dans des logs",
    prompt: "Analyse ces logs et trouve la cause de l'erreur : ",
    systemPrompt:
      "You are an ops engineer. Read logs carefully, explain the root cause and suggest a fix, in the language of the user.",
    builtin: true,
  },
  {
    id: "rediger",
    name: "Rédiger",
    alias: "rediger",
    description: "Rédige un texte, un email ou un document",
    prompt: "Rédige le texte suivant : ",
    systemPrompt:
      "You are a careful writer. Be clear, structured and concise. Favour short paragraphs and useful headings.",
    builtin: true,
  },
  {
    id: "traduire",
    name: "Traduire",
    alias: "traduire",
    description: "Traduis un texte vers une autre langue",
    prompt: "Traduis le texte suivant : ",
    systemPrompt:
      "You are a professional translator. Preserve meaning, tone and formatting; output only the target language.",
    builtin: true,
  },
  {
    id: "idees",
    name: "Brainstormer",
    alias: "idees",
    description: "Propose des idées et des alternatives",
    prompt: "Propose-moi des idées à partir de ce sujet : ",
    systemPrompt:
      "You brainstorm broadly: list varied and creative options, then a short recommendation.",
    builtin: true,
  },
  {
    id: "relecture",
    name: "Relecture",
    alias: "relecture",
    description: "Relis, corrige et améliore un texte",
    prompt: "Relis le texte suivant, corrige les fautes et améliore le style : ",
    systemPrompt:
      "You proofread carefully: fix errors, improve clarity and style, then briefly explain the main changes.",
    builtin: true,
  },
];

export function loadCustomSkills(): Skill[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SKILLS_KEY) || "[]") as Skill[];
    return Array.isArray(raw) ? raw.filter((s) => s && s.id && s.name) : [];
  } catch {
    return [];
  }
}

export function saveCustomSkills(list: Skill[]) {
  try {
    localStorage.setItem(SKILLS_KEY, JSON.stringify(list));
  } catch {
    /* stockage indisponible : on ignore */
  }
}

/** Filtre les compétences par la requête tapée après le « / ». */
export function skillMatches(s: Skill, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    s.name.toLowerCase().includes(q) ||
    s.alias.toLowerCase().includes(q) ||
    s.description.toLowerCase().includes(q)
  );
}
