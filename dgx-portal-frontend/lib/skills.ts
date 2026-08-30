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
    name: "Summarize",
    alias: "summarize",
    description: "Condense un texte en points clés",
    prompt: "Résume ce texte en 3 points clairs et concis : ",
    systemPrompt:
      "Tu es un synthétiseur précis. Ne garde que les points clés, dans la langue du texte, et reste concis.",
    builtin: true,
  },
  {
    id: "expliquer",
    name: "Explain",
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
    name: "Analyze logs",
    alias: "logs",
    description: "Trouve la cause d'une erreur dans des logs",
    prompt: "Analyse ces logs et trouve la cause de l'erreur : ",
    systemPrompt:
      "Tu es un ingénieur d'exploitation. Lis les logs avec attention, explique la cause racine et suggère un correctif, dans la langue de l'utilisateur.",
    builtin: true,
  },
  {
    id: "rediger",
    name: "Write",
    alias: "write",
    description: "Rédige un texte, un email ou un document",
    prompt: "Rédige le texte suivant : ",
    systemPrompt:
      "Tu es un rédacteur soigneux. Sois clair, structuré et concis. Privilégie les paragraphes courts et les titres utiles.",
    builtin: true,
  },
  {
    id: "traduire",
    name: "Translate",
    alias: "translate",
    description: "Traduis un texte vers une autre langue",
    prompt: "Traduis le texte suivant : ",
    systemPrompt:
      "Tu es un traducteur professionnel. Préserve le sens, le ton et la mise en forme ; n'affiche que la langue cible.",
    builtin: true,
  },
  {
    id: "idees",
    name: "Brainstorm",
    alias: "brainstorm",
    description: "Propose des idées et des alternatives",
    prompt: "Propose-moi des idées à partir de ce sujet : ",
    systemPrompt:
      "Tu proposes des idées de façon large : liste des options variées et créatives, puis une brève recommandation.",
    builtin: true,
  },
  {
    id: "relecture",
    name: "Proofread",
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
