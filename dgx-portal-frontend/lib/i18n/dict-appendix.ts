/**
 * English dictionary — entries appended over time: auth/settings continuations, tool-call labels, thinking indicator and backend messages.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictAppendix: Record<string, string> = {
  // — Login (continued) —
  "Session expirée — recharge la page et réessaie.": "Session expired — reload the page and try again.",
  "Identifiants incorrects.": "Incorrect credentials.",
  "Appareil inconnu": "Unknown device",
  "Origine non enregistrée : session ouverte avant que le portail ne note l'IP et le navigateur.":
    "Origin not recorded: this session was opened before the portal started recording the IP and browser.",
  "Accès révoqué pour ce compte. Contacte un administrateur.":
    "Access revoked for this account. Contact an administrator.",

  // — Settings: MCP / Skills (continued) —
  "Serveur MCP mis à jour": "MCP server updated",
  "Serveur MCP connecté": "MCP server connected",
  "outil(s) trouvé(s)": "tool(s) found",
  "Serveur activé": "Server enabled",
  "Exemple : notion_workspace": "Example: notion_workspace",
  "Exemple : analyse-de-logs": "Example: log-analysis",

  // — Playground (continued) —
  "Vous": "You",

  // — Leaderboard (continued) —
  "toi": "you",

  // — Home / activity (continued) —

  // — Settings: Usage (continued) —
  "% utilisé": "% used",

  // — Support (continued) —
  "Bonjour 👋 Je suis **{name}**, l'assistant de la plateforme. Je peux te dépanner (clé, quota, modèle, intégration OpenCode/Hermes…) mais aussi **agir pour toi** : créer une clé, demander du budget, demander un modèle. Dis-moi ce qu'il te faut.":
    "Hi 👋 I'm **{name}**, the platform's assistant. I can help you troubleshoot (keys, quota, models, OpenCode/Hermes integration…) but I can also **act on your behalf**: create a key, request budget, request a model. Tell me what you need.",

  // — Tool-call labels (Support assistant) —
  "Révoquer une clé API": "Revoke an API key",
  "Lancer un modèle": "Launch a model",
  "Arrêter le modèle": "Stop the model",
  "Action bloquée après lecture d'un contenu externe.": "Action blocked after reading external content.",

  // — Leaderboard (continued 2) —
  "Jour": "Day",
  "Semaine": "Week",
  "Mois": "Month",
  "compte actif": "active account",
  "comptes actifs": "active accounts",
  "Aujourd'hui": "Today",
  "Source": "Source",
  "Externe": "External",
  "SSO": "SSO",
  "Local": "Local",
  "Géré": "Managed",
  "Ici": "Here",
  "via": "via",
  "Crée et gère les comptes locaux, les groupes, les quotas et les droits.": "Create and manage local accounts, groups, quotas and rights.",
  "Identifiant": "Username",
  "Créer": "Create",
  "Non": "No",
  "Oui": "Yes",
  "Utilisateurs": "Users",
  "Comptes locaux gérés ici (mots de passe hachés). Le quota vient de la surcharge de l'utilisateur, sinon du groupe, sinon du défaut global.": "Local accounts managed here (hashed passwords). Quota comes from the user's override, else the group, else the global default.",
  "Nom complet": "Full name",
  "Quota (vide = groupe/défaut)": "Quota (empty = group/default)",
  "Aucun groupe": "No group",
  "Groupe": "Group",
  // « par jour » suffix appended after a number (card of a group).
  "/ j": "/ d",
  "Actif": "Active",
  "Désactivé": "Disabled",
  "Actions": "Actions",
  "Retirer admin": "Remove admin",
  "Rendre admin": "Make admin",
  "Nouveau mot de passe": "New password",
  "Groupes": "Groups",
  "quota par défaut": "default quota",
  "Supprimer ce groupe ?": "Delete this group?",
  "Nom du groupe": "Group name",
  "Admin par défaut": "Default admin",
  "Ajouter le groupe": "Add group",
  "Échec de l'action.": "Action failed.",
  "Génération moy.": "Avg. generation",
  "Extraction moy.": "Avg. extraction",
  "Dernière": "Last",
  "Caractères/doc": "Chars/doc",
  "Facteur temps réel": "Real-time factor",
  "Vitesse de synthèse": "Synthesis speed",
  "Taux de réussite": "Success rate",
  "Vidéo générée auj.": "Video made today",
  "Calcul / s de vidéo": "Compute / s of video",
  "7 derniers jours": "Last 7 days",
  "30 derniers jours": "Last 30 days",
  "hier": "yesterday",
  "la semaine précédente": "the previous week",
  "les 30 jours précédents": "the previous 30 days",

  // — Thinking indicator —
  "Cogitation": "Cogitating",
  "Rumination": "Ruminating",
  "Gamberge": "Pondering",
  "Mijotage": "Simmering",
  "Élucubration": "Musing",
  "Ébullition": "Percolating",
  "Méditation": "Meditating",
  "Tergiversation": "Waffling",
  "Concoction": "Concocting",

  // — Backend messages: MCP/skills/admin/media/voice/transcription/maintenance (shown via t(res.error)) —
  "Nom et URL requis.": "Name and URL required.",
  "Connexion au serveur MCP impossible.": "Could not connect to the MCP server.",
  "Tu as déjà un serveur MCP avec ce nom.": "You already have an MCP server with this name.",
  "Serveur introuvable.": "Server not found.",
  "URL invalide.": "Invalid URL.",
  "L'URL doit être en https://.": "The URL must use https://.",
  "URL invalide (pas d'hôte).": "Invalid URL (no host).",
  "Cet hôte n'est pas autorisé.": "This host is not allowed.",
  "Nom d'hôte introuvable.": "Hostname not found.",
  "Cette adresse pointe vers un réseau interne/privé, refusée.": "This address points to an internal/private network and is refused.",
  "Nom, description et instructions requis.": "Name, description and instructions required.",
  "Tu as déjà une compétence avec ce nom.": "You already have a skill with this name.",
  "Compétence introuvable.": "Skill not found.",
  "Thème inconnu.": "Unknown theme.",
  "Langue inconnue.": "Unknown language.",
  "Modèle OCR introuvable.": "OCR model not found.",
  "Identifiant invalide (a-z, 0-9, . _ - , max 64).": "Invalid username (a-z, 0-9, . _ - , max 64).",
  "Cet utilisateur existe déjà.": "This user already exists.",
  "Groupe inconnu.": "Unknown group.",
  "Utilisateur introuvable.": "User not found.",
  "Quota invalide.": "Invalid quota.",
  "Un prompt texte est requis.": "A text prompt is required.",
  "ComfyUI inaccessible ou requête refusée.": "ComfyUI unreachable or request refused.",
  "Aucune image fournie.": "No image provided.",
  "Format d'image non supporté (PNG/JPEG/WebP uniquement).": "Unsupported image format (PNG/JPEG/WebP only).",
  "Image trop volumineuse (15 Mo max).": "Image too large (15 MB max).",
  "Aucun audio fourni.": "No audio provided.",
  "Enregistrement trop volumineux (15 Mo max).": "Recording too large (15 MB max).",
  "La transcription a mis trop de temps.": "Transcription took too long.",
  "Service de transcription injoignable.": "Transcription service unreachable.",
  "Mode maintenance en cours — réessaie plus tard.": "Maintenance in progress — try again later.",
};
