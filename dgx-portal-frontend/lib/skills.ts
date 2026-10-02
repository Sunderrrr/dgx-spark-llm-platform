// Claude-style skills: a skill = a /alias command that prepares the
// model with a prompt to send (+ an optional system prompt). The
// built-in skills are bundled; user-created skills live in
// localStorage (like the snippets).

export type Skill = {
  id: string;
  /** Displayed label (FR msgid for the built-in skills). */
  name: string;
  /** /alias command (e.g. "resumer" -> type /resumer). */
  alias: string;
  /** Subtitle displayed in the menu. */
  description: string;
  /** Text put into the field on selection. */
  prompt: string;
  /** System prompt applied on selection (behaviour of the model). */
  systemPrompt?: string;
  builtin?: boolean;
};

export const SKILLS_KEY = "cronos.skills";

export const BASE_SKILLS: Skill[] = [
  {
    id: "resumer",
    name: "Résumer",
    alias: "summarize",
    description: "Condense un texte en points clés",
    prompt: "Résume ce texte en 3 points clairs et concis : ",
    systemPrompt:
      "Tu es un synthétiseur précis. Ne garde que les points clés, dans la langue du texte, et reste concis.",
    builtin: true,
  },
  {
    id: "expliquer",
    name: "Expliquer",
    alias: "explain",
    description: "Décompose un sujet technique simplement",
    prompt: "Explique-moi ce sujet simplement, comme à un débutant : ",
    systemPrompt:
      "Tu expliques clairement, avec des analogies simples et sans jargon. Considère que le lecteur est intelligent mais nouveau sur le sujet.",
    builtin: true,
  },
  {
    id: "coder",
    name: "Code",
    alias: "code",
    description: "Génère une fonction, un script ou un test",
    prompt: "Écris le code suivant, complet et exécutable : ",
    systemPrompt:
      "Tu es un ingénieur logiciel senior. Produis du code complet, exécutable et idiomatique. Privilégie des fichiers entiers plutôt que des extraits et n'élide jamais une partie d'un fichier.",
    builtin: true,
  },
  {
    id: "logs",
    name: "Analyser des logs",
    alias: "logs",
    description: "Trouve la cause d'une erreur dans des logs",
    prompt: "Analyse ces logs et trouve la cause de l'erreur : ",
    systemPrompt:
      "Tu es un ingénieur d'exploitation. Lis les logs avec attention, explique la cause racine et suggère un correctif, dans la langue de l'utilisateur.",
    builtin: true,
  },
  {
    id: "rediger",
    name: "Rédiger",
    alias: "write",
    description: "Rédige un texte, un email ou un document",
    prompt: "Rédige le texte suivant : ",
    systemPrompt:
      "Tu es un rédacteur soigneux. Sois clair, structuré et concis. Privilégie les paragraphes courts et les titres utiles.",
    builtin: true,
  },
  {
    id: "traduire",
    name: "Traduire",
    alias: "translate",
    description: "Traduis un texte vers une autre langue",
    prompt: "Traduis le texte suivant : ",
    systemPrompt:
      "Tu es un traducteur professionnel. Préserve le sens, le ton et la mise en forme ; n'affiche que la langue cible.",
    builtin: true,
  },
  {
    id: "idees",
    name: "Imaginer",
    alias: "brainstorm",
    description: "Propose des idées et des alternatives",
    prompt: "Propose-moi des idées à partir de ce sujet : ",
    systemPrompt:
      "Tu proposes des idées de façon large : liste des options variées et créatives, puis une brève recommandation.",
    builtin: true,
  },
  {
    id: "relecture",
    name: "Relire",
    alias: "proofread",
    description: "Relis, corrige et améliore un texte",
    prompt: "Relis le texte suivant, corrige les fautes et améliore le style : ",
    systemPrompt:
      "Tu fais une relecture attentive : corrige les erreurs, améliore la clarté et le style, puis explique brièvement les principaux changements.",
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
    /* storage unavailable: we ignore */
  }
}

/** Filters the skills by the query typed after the « / ». */
export function skillMatches(s: Skill, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    s.name.toLowerCase().includes(q) ||
    s.alias.toLowerCase().includes(q) ||
    s.description.toLowerCase().includes(q)
  );
}
