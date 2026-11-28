/**
 * English dictionary — settings pages: usage, appearance, personalization, MCP and skills.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictSettings: Record<string, string> = {
  // — Usage —
  "Vos limites d'utilisation": "Your usage limits",
  "Suivez la consommation de votre compte sur chaque quota disponible.":
    "Track your account's consumption against each available quota.",
  "Rafraîchir": "Refresh",
  "Illimité": "Unlimited",
  "utilisé": "used",
  "Budget de tokens": "Token budget",
  "Quota quotidien partagé par toutes tes clés API.":
    "Daily quota shared across all your API keys.",
  "Messages Support": "Support messages",
  "Messages Playground": "Playground messages",
  "Conversations enregistrées": "Saved conversations",
  "Au-delà, les plus anciennes sont supprimées automatiquement.":
    "Beyond this, the oldest ones are deleted automatically.",
  "Serveurs MCP connectés": "Connected MCP servers",
  "Serveurs distants dont l'assistant peut utiliser les outils.":
    "Remote servers whose tools the assistant can use.",
  "Compétences définies": "Defined skills",
  "Instructions réutilisables chargées à la demande.":
    "Reusable instructions loaded on demand.",
  "Fenêtre de contexte du modèle": "Model context window",
  "Aucun modèle actif.": "No active model.",
  "Besoin d'augmenter tes limites ? Demande plus de tokens depuis l'onglet « Clés API », ou passe par l'assistant Support.":
    "Need higher limits? Request more tokens from the “API keys” tab, or ask the Support assistant.",

  // — Appearance —
  "THÈME": "THEME",
  "Ajuste l'apparence de l'interface.": "Adjust the appearance of the interface.",
  "Clair": "Light",
  "Sombre": "Dark",
  "Système": "System",
  "COULEUR D'ACCENT": "ACCENT COLOUR",
  "Change la couleur principale de l'interface.":
    "Change the interface's primary colour.",
  "LANGUE": "LANGUAGE",
  "Choisis la langue de l'interface.": "Choose the interface language.",
  "Français": "French",
  "Anglais": "English",
  "Neutre": "Neutral",
  "Indigo": "Indigo",
  "Violet": "Violet",
  "Rose": "Rose",
  "Ambre": "Amber",
  "Émeraude": "Emerald",
  "Cyan": "Cyan",
  "Ardoise": "Slate",
  "Brique": "Brick",
  "Prune": "Plum",

  // — Personalization —
  "Par défaut, ton avatar est créé à partir de ton pseudo. Tu peux aussi choisir un logo de marque d'IA — pas d'import d'image personnelle.":
    "Your avatar is generated from your username by default. You can also pick an AI brand logo — no personal image upload.",
  "Généré depuis mon pseudo": "Generated from my username",

  // — MCP —
  "Connecte un serveur MCP distant en HTTPS : ses outils deviennent utilisables par l'assistant Support.":
    "Connect a remote MCP server over HTTPS: its tools become usable by the Support assistant.",
  "Connecter un MCP": "Connect an MCP",
  "Connecter un MCP personnalisé": "Connect a custom MCP",
  "Modifier le serveur MCP": "Edit MCP server",
  "Configurez la connexion et la façon dont ses outils peuvent être utilisés.":
    "Configure the connection and how its tools may be used.",
  "Aucun serveur MCP connecté.": "No MCP server connected.",
  "Connecte un serveur pour étendre les capacités de l'assistant.":
    "Connect a server to extend the assistant's capabilities.",
  "Nom": "Name",
  "URL du serveur": "Server URL",
  "Lettres, chiffres, underscores et tirets uniquement.":
    "Letters, digits, underscores and hyphens only.",
  "Description (optionnel)": "Description (optional)",
  "Ce que fournit ce serveur": "What this server provides",
  "Outils autorisés (optionnel)": "Allowed tools (optional)",
  "Séparez par des virgules. Vide = tous les outils.":
    "Comma-separated. Empty = all tools.",
  "Autorisation (optionnel)": "Authorization (optional)",
  "Bearer token ou secret": "Bearer token or secret",
  "Envoyé en en-tête Authorization.": "Sent as the Authorization header.",
  "Laisser vide pour conserver le secret actuel ; « - » pour le retirer.":
    "Leave empty to keep the current secret; “-” to remove it.",
  "Enregistrer le serveur": "Save server",
  "Mettre à jour le serveur": "Update server",
  "Serveur MCP supprimé.": "MCP server deleted.",
  "Outils autorisés :": "Allowed tools:",
  "Auth": "Auth",

  // — Skills —
  "Des instructions réutilisables que tu écris toi-même ; l'assistant les charge quand elles sont utiles à ta demande.":
    "Reusable instructions you write yourself; the assistant loads them when relevant to your request.",
  "Nouvelle compétence": "New skill",
  "Créer une compétence": "Create a skill",
  "Modifier la compétence": "Edit skill",
  "L'assistant chargera ces instructions en contexte quand la compétence s'applique.":
    "The assistant will load these instructions into context when the skill applies.",
  "Aucune compétence pour l'instant.": "No skills yet.",
  "Crée une compétence pour guider l'assistant sur une tâche récurrente.":
    "Create a skill to guide the assistant on a recurring task.",
  "Description": "Description",
  "Quand l'utiliser, en une phrase": "When to use it, in one sentence",
  "Instructions": "Instructions",
  "Instructions détaillées que l'assistant chargera en contexte…":
    "Detailed instructions the assistant will load into context…",
  "Enregistrer la compétence": "Save skill",
  "Mettre à jour la compétence": "Update skill",
  "Compétence enregistrée.": "Skill saved.",
  "Compétence mise à jour.": "Skill updated.",
  "Compétence supprimée.": "Skill deleted.",
  "Échec de l'enregistrement.": "Failed to save.",
  "Échec de la connexion au serveur MCP.": "Failed to connect to the MCP server.",
};
