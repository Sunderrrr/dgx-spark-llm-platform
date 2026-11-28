/**
 * English dictionary — support assistant, model discovery and leaderboard.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictSupport: Record<string, string> = {
  // — Support —
  "Un assistant IA connecté à la plateforme : il voit tes clés (masquées), ton budget et l'état du serveur pour t'aider en cas de pépin.":
    "An AI assistant wired into the platform: it sees your keys (masked), your budget and the server status to help when something goes wrong.",
  "aucun modèle actif": "no active model",
  "Pas de réponse.": "No response.",
  "Erreur réseau — réessaie.": "Network error — try again.",
  "Écris ton message…  (Entrée pour envoyer, Maj+Entrée pour un saut de ligne)":
    "Write your message…  (Enter to send, Shift+Enter for a new line)",
  "L'assistant ne voit que tes données (clés masquées). Ne colle jamais une clé complète ici.":
    "The assistant only sees your data (masked keys). Never paste a full key here.",
  "Créer une clé": "Create a key",
  "Génère une nouvelle clé API pour tes intégrations": "Generate a new API key for your integrations",
  "Crée-moi une clé API pour mon laptop": "Create an API key for my laptop",
  "Demander du budget": "Request budget",
  "Augmente ton quota de tokens mensuel": "Raise your monthly token quota",
  "Demande plus de budget pour mon compte": "Request more budget for my account",
  "Modèles disponibles": "Available models",
  "Liste les modèles actifs et leur fenêtre de contexte": "List active models and their context window",
  "Quels modèles je peux utiliser et quel est leur contexte ?":
    "Which models can I use and what is their context?",
  "Erreur 401": "401 error",
  "Diagnostique un problème d'authentification": "Diagnose an authentication problem",
  "Ma clé API renvoie une erreur 401, pourquoi ?": "My API key returns a 401 error, why?",

  // — Search for a model —
  "Recherche en direct dans tout Hugging Face. Demande ensuite le lancement du modèle sur le DGX.":
    "Live search across all of Hugging Face. Then request the model be launched on the DGX.",
  "Recherche": "Search",
  "Nom de modèle, ex: Qwen, Ornith, Mistral...": "Model name, e.g. Qwen, Ornith, Mistral...",
  "Tâche": "Task",
  "Toutes les tâches": "All tasks",
  "Chercher": "Search",
  "Charger plus": "Load more",
  "Seulement testés sur DGX Spark (GB10)": "Only tested on DGX Spark (GB10)",
  "Filtre actif : seuls les modèles portant le tag GB10 — testés sur DGX Spark — sont affichés.":
    "Filter on: only models tagged GB10 — tested on DGX Spark — are shown.",
  "Toutes les tâches, tout le catalogue. Le tag GB10 est signalé par un badge quand le modèle le porte.":
    "All tasks, the whole catalog. A GB10 badge marks the models that carry the tag.",
  "Sans jeton Hugging Face, les dépôts à accès restreint n'apparaissent pas dans les résultats.":
    "Without a Hugging Face token, gated repositories do not show up in the results.",
  "La recherche porte sur le nom du dépôt, pas sur sa description : vérifie l'orthographe.":
    "The search matches the repository name, not its description: check the spelling.",
  "Chercher sur Hugging Face": "Search on Hugging Face",
  "modèles affichés": "models shown",
  "résultats pour": "results for",
  "les plus téléchargés de Hugging Face": "the most downloaded on Hugging Face",
  "Tape un nom de modèle pour explorer Hugging Face.": "Type a model name to explore Hugging Face.",
  "Demander": "Request",

  // — Request a model —
  "Identifiant HuggingFace *": "HuggingFace identifier *",
  "Format : organisation/nom-du-modèle": "Format: organisation/model-name",
  "Pourquoi ce modèle ? (optionnel)": "Why this model? (optional)",
  "Ex : tester les capacités de raisonnement, comparer avec Ornith...":
    "E.g. test reasoning capabilities, compare with Ornith...",
  "Envoyer la demande": "Send request",
  "Demande envoyée !": "Request sent!",
  "L'admin est notifié par Discord et email. Le statut apparaît sur ta page d'accueil.":
    "The admin is notified by Discord and email. The status appears on your home page.",
  "Tu ne connais pas l'ID exact ? ": "Don't know the exact ID? ",
  "Cherche sur HuggingFace →": "Search on HuggingFace →",

  // — Leaderboard —
  "Période": "Period",
  "Aucune consommation sur cette période.": "No consumption over this period.",
  "Tokens réellement consommés sur la période, par compte.":
    "Real tokens used over the period, per account.",
  "Rang": "Rank",
  "Compte": "Account",
  "Part du total": "Share of total",
  "Tendance": "Trend",
  "Activité sur la période": "Activity over the period",
  "Évolution": "Change",
  "Évolution vs {p}": "Change vs {p}",
  "Total": "Total",
  "Généré": "Generated",
  "Entrée": "Input",
  "Métrique": "Metric",
  "dont {n} générés": "{n} of it generated",
  "Votre position": "Your rank",
  "sur {n} comptes actifs": "across {n} active accounts",
  "Vous n'avez rien consommé sur cette période.": "You have not used anything over this period.",
  "tokens au total": "tokens in total",
  "Classement indisponible.": "Leaderboard unavailable.",
  "La consommation n'a pas pu être lue. Réessaie dans un instant.":
    "Consumption could not be read. Try again in a moment.",
  "Total = entrée + généré.": "Total = input + generated.",
  "« Depuis le début » n'a pas de période précédente : aucune évolution à afficher.":
    "\"Since the beginning\" has no previous period: no change to show.",
  "Les lignes non attribuées ne correspondent à aucun compte (clés supprimées ou de test) : elles consomment sans être classées.":
    "Unattributed rows match no account (deleted or test keys): they consume without being ranked.",
  "Clés non attribuées": "Unattributed keys",
};
