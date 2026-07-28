"use client";

import { createContext, useCallback, useContext } from "react";

/** Langues proposées dans Réglages → Apparence. */
export type Lang = "fr" | "en";

/* Le texte FRANÇAIS sert de clé de traduction (comme un msgid gettext).
 * L'application a été écrite en français : garder les littéraux français
 * dans le code évite d'inventer des centaines d'identifiants opaques, laisse
 * les fichiers lisibles, et rend le mode français strictement identité — donc
 * impossible à casser par une clé oubliée. Seul l'anglais a besoin d'un
 * dictionnaire ; une entrée manquante retombe sur le français plutôt que
 * d'afficher un nom de clé. */
const EN: Record<string, string> = {
  // — Navigation et coque —
  "Accueil": "Home",
  "Playground": "Playground",
  "Chercher un modèle": "Find a model",
  "Demander un modèle": "Request a model",
  "Classement": "Leaderboard",
  "Support": "Support",
  "Admin": "Admin",
  "Réglages": "Settings",
  "Basculer le thème": "Toggle theme",
  "Déconnexion": "Log out",
  "Fermer": "Close",
  "Retour": "Back",
  "Annuler": "Cancel",
  "Supprimer": "Delete",
  "Modifier": "Edit",
  "Ajouter": "Add",
  "Envoyer": "Send",
  "Enregistrer": "Save",
  "Copier": "Copy",
  "Régénérer": "Regenerate",
  "Éditer": "Edit",

  // — Réglages : rail —
  "Réglages du compte": "Account settings",
  "Réglages de l'app": "App settings",
  "Mon compte": "My account",
  "Usage": "Usage",
  "Clés API": "API keys",
  "Personnalisation": "Personalisation",
  "Apparence": "Appearance",
  "MCP": "MCP",
  "Compétences": "Skills",
  "Clés API, serveurs MCP, compétences et personnalisation.":
    "API keys, MCP servers, skills and personalisation.",

  // — Mon compte —
  "Tokens totaux": "Total tokens",
  "Pic journalier": "Daily peak",
  "Jours actifs": "Active days",
  "Moyenne / jour": "Average / day",
  "ACTIVITÉ TOKENS": "TOKEN ACTIVITY",
  "6 derniers mois": "Last 6 months",
  "Insights d'activité": "Activity insights",
  "Répartition tokens": "Token breakdown",
  "Total période": "Period total",
  "Entrée (prompt)": "Input (prompt)",
  "Sortie (généré)": "Output (generated)",
  "Clés API actives": "Active API keys",
  "Budget": "Budget",
  "Budget illimité (admin)": "Unlimited budget (admin)",
  "Consommé aujourd'hui": "Used today",
  "Activité de tokens par jour": "Daily token activity",

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

  // — Apparence —
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

  // — Personnalisation —
  "Choisis un avatar parmi les logos proposés — pas d'import d'image personnelle.":
    "Pick an avatar from the logos below — no personal image upload.",

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

  // — Compétences —
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

  // — Accueil —
  "Bonjour": "Hello",
  "Ton accès self-service à l'inférence LLM sur DGX Spark.":
    "Your self-service access to LLM inference on the DGX Spark.",
  "Mes clés API": "My API keys",
  "Modèles disponibles maintenant": "Models available now",
  "Aucun modèle actif": "No active model",
  "Demande le lancement d'un modèle.": "Request that a model be launched.",
  "En ligne": "Online",
  "Créer une clé API": "Create an API key",
  "État du serveur": "Server status",
  "Modèle actif": "Active model",
  "aucun": "none",
  "en ligne": "online",
  "arrêté": "stopped",
  "Débit": "Throughput",
  "Sessions": "Sessions",
  "Requêtes servies": "Requests served",
  "Qui utilise le modèle · 2 dernières min · visible admin uniquement":
    "Who's using the model · last 2 min · admin only",
  "Personne n'utilise le modèle en ce moment.":
    "No one is using the model right now.",
  "Mon utilisation — aujourd'hui": "My usage — today",
  "Tokens · 24 h": "Tokens · 24h",
  "Clés actives": "Active keys",
  "Crée des clés personnelles pour accéder aux modèles via l'API OpenAI-compatible.":
    "Create personal keys to access models through the OpenAI-compatible API.",
  "Limite :": "Limit:",
  "Illimitée (admin)": "Unlimited (admin)",
  "Gérer mes clés": "Manage my keys",
  "Catalogue HuggingFace": "HuggingFace catalog",
  "Parcours les modèles disponibles et demande le lancement de celui qui t'intéresse.":
    "Browse available models and request the one you're interested in.",
  "Explorer les modèles": "Explore models",
  "Tu connais un modèle que tu veux tester ? Envoie une demande à l'admin.":
    "Know a model you want to try? Send a request to the admin.",
  "Faire une demande": "Send a request",
  "Mes dernières demandes": "My recent requests",
  "Modèle": "Model",
  "Raison": "Reason",
  "Statut": "Status",
  "Date": "Date",
  "En attente": "Pending",
  "Lancé": "Launched",
  "Refusé": "Rejected",

  // — Playground / Support —
  "Réflexion": "Thinking",
  "Lecture du fichier…": "Reading the file…",
  "Nouvelle conversation": "New conversation",
  "Historique": "History",
  "Aucune conversation": "No conversations",
  "Supprimer cette conversation": "Delete this conversation",
  "Exporter en Markdown": "Export as Markdown",
  "Joindre un fichier": "Attach a file",
  "Fichiers joints": "Attached files",
  "Fichiers texte uniquement. Les tokens comptent sur ton budget.":
    "Text files only. Tokens count against your budget.",
  "Raisonnement": "Reasoning",
  "Sur quoi veux-tu travailler ?": "What do you want to work on?",
};

const I18nContext = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({
  lang: "fr",
  setLang: () => {},
});

export function I18nProvider({
  lang,
  setLang,
  children,
}: {
  lang: Lang;
  setLang: (l: Lang) => void;
  children: React.ReactNode;
}) {
  return <I18nContext.Provider value={{ lang, setLang }}>{children}</I18nContext.Provider>;
}

export function useLang() {
  return useContext(I18nContext);
}

/** t("texte français") — identité en FR, traduction en EN. */
export function useT() {
  const { lang } = useContext(I18nContext);
  return useCallback((fr: string) => (lang === "en" ? EN[fr] ?? fr : fr), [lang]);
}
