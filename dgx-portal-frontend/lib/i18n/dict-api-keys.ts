/**
 * English dictionary — API keys page and integration snippets.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictApiKeys: Record<string, string> = {
  // — API keys —
  "Des clés personnelles pour appeler les modèles via l'API compatible OpenAI.":
    "Personal keys to call the models through the OpenAI-compatible API.",
  "Nouvelle clé": "New key",
  "Nom (ex: mon-laptop)": "Name (e.g. my-laptop)",
  "Endpoint :": "Endpoint:",
  "— compatible OpenAI.": "— OpenAI-compatible.",
  "Budget du compte — partagé par toutes tes clés /": "Account budget — shared across all your keys /",
  "tokens restants": "tokens remaining",
  "Demande en attente": "Request pending",
  "Demander plus de tokens": "Request more tokens",
  "Raison (optionnel)": "Reason (optional)",
  "Aucune clé pour l'instant.": "No keys yet.",
  "Utilise « Nouvelle clé » en haut à droite pour en générer une.":
    "Use “New key” at the top right to generate one.",
  "Alias": "Alias",
  "Clé": "Key",
  "Afficher": "Show",
  "Masquer": "Hide",
  "Dépensé": "Spent",
  "tokens": "tokens",
  // Singular for « 1 token »: the plural is chosen at render time, with the
  // pattern already in place in the repo (t(n > 1 ? "tokens" : "token")).
  "token": "token",
  "Révoquer": "Revoke",
  "Intégrations": "Integrations",
  // Integration tab: the only label in the list that is not a proper noun (the
  // other tools keep their proper name). The `value` stays "env", it is technical.
  // Comments inside the integration snippets (lib/integrationSnippets.ts).
  "Contexte du modèle : {context} tokens (sortie max {output}).": "Model context: {context} tokens (max output {output}).",
  "Le proxy expose l'API Anthropic sur /v1/messages : Claude Code s'y branche\n# directement, sans passerelle ni adaptateur.":
    "The proxy serves the Anthropic API on /v1/messages: Claude Code plugs into it\n# directly, with no gateway or adapter.",
  "Indispensable : sans ces deux lignes Claude Code réclame ses modèles\n# « claude-… », que le proxy ne connaît pas — il répond 400 « Invalid model name ».":
    "Required: without these two lines Claude Code asks for its own\n# \"claude-…\" models, which the proxy does not know — it answers 400 \"Invalid model name\".",
  "À savoir : le modèle est un modèle ouvert auto-hébergé, pas un modèle Anthropic.\n# Le fonctionnement est le même (outils, édition de fichiers, commandes), mais la\n# qualité sur les tâches longues et l'enchaînement d'outils reste en deçà.":
    "Good to know: the model is a self-hosted open model, not an Anthropic model.\n# It works the same way (tools, file edits, commands), but quality on long\n# tasks and tool chaining stays below.",
  "Option A — assistant interactif": "Option A — interactive wizard",
  "choisir « Custom endpoint (self-hosted / vLLM / etc.) »": "pick \"Custom endpoint (self-hosted / vLLM / etc.)\"",
  "Option B — éditer ~/.hermes/config.yaml": "Option B — edit ~/.hermes/config.yaml",
  "ajouter dans « models »": "add to \"models\"",
  "Bonjour !": "Hello!",
  "Variables d'env.": "Env vars",
  "Masquer la clé": "Hide key",
  "Révéler la clé": "Reveal key",
  "Clé créée !": "Key created!",
  "Clé révoquée.": "Key revoked.",
  "Demande de tokens envoyée !": "Token request sent!",
};
