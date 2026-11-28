/**
 * English dictionary — sentences returned by the server, translated at display time (tServer).
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictServer: Record<string, string> = {
  // ── Server sentences, translated at DISPLAY time (progressive migration).
  //    The keys are the server strings BYTE-IDENTICAL (dgx-portal/*.py —
  //    `_json_erreur(…)`, `jsonify({'error': …})`, sidecar `detail`), one
  //    character off and the lookup silently falls back to French. Sentences
  //    with a runtime value keep the placeholder (« Lancement de {name} … »):
  //    tServeur() re-injects the captured value with .replace(), like the
  //    quota notice in lib/notices.ts. ──
  // Login / auth / sessions
  "Trop de tentatives. Réessaie dans {wait} min.": "Too many attempts. Try again in {wait} min.",
  "Trop de tentatives, réessaie dans {wait} s.": "Too many attempts — try again in {wait} s.",
  "Trop de requêtes. Réessaie dans {wait} s.": "Too many requests. Try again in {wait} s.",
  "Accès réservé aux administrateurs.": "Administrators only.",
  "CSRF token manquant ou invalide.": "Missing or invalid CSRF token.",
  "Ce compte exige sa clé de sécurité : connecte-toi avec le mot de passe, puis valide la clé.":
    "This account requires its security key: sign in with the password, then validate the key.",
  "Accès révoqué pour ce compte.": "Access revoked for this account.",
  "Mot de passe incorrect.": "Incorrect password.",
  "Mot de passe requis.": "Password required.",
  "Mot de passe requis pour supprimer ton compte.": "Password required to delete your account.",
  "Mot de passe requis pour ajouter une clé.": "Password required to add a key.",
  "Demande de connexion expirée ou invalide.": "Sign-in request expired or invalid.",
  "Demande d'enregistrement expirée ou invalide.": "Registration request expired or invalid.",
  "Compte incohérent.": "Inconsistent account.",
  "Session introuvable (déjà fermée ?).": "Session not found (already closed?).",
  "Identifiant de session invalide.": "Invalid session identifier.",
  "Identifiant de session ambigu, réessaie.": "Ambiguous session identifier — try again.",
  "Clé refusée : la vérification a échoué.": "Key refused: verification failed.",
  "Clé invalide.": "Invalid key.",
  "Réponse de clé manquante.": "Missing key response.",
  "Clé inconnue pour ce compte.": "Unknown key for this account.",
  "Enregistre d'abord une clé de sécurité.": "Register a security key first.",
  "Compte SSO : aucun mot de passe n'est géré par le portail, la vérification est impossible.":
    "SSO account: the portal manages no password, verification is impossible.",
  "Compte géré par l'annuaire (LDAP/SSO) : le mot de passe se change là-bas.":
    "Directory-managed account (LDAP/SSO): the password is changed there.",
  "Le nouveau mot de passe est identique à l'actuel.": "The new password is the same as the current one.",
  "Confirmation requise : cette suppression est définitive.": "Confirmation required: this deletion is permanent.",
  // API keys
  "Clé introuvable sur ce compte.": "Key not found on this account.",
  "Tu as déjà une clé nommée ainsi.": "You already have a key with that name.",
  "La clé n'a pas pu être créée : le service de clés (LiteLLM) n'a pas répondu. Réessaie dans un instant.":
    "The key could not be created: the key service (LiteLLM) did not respond. Try again in a moment.",
  "La clé n'a PAS pu être révoquée : LiteLLM n'a pas répondu. Elle est encore valide, réessaie.":
    "The key could NOT be revoked: LiteLLM did not respond. It is still valid — try again.",
  "Le nom n'a PAS pu être changé : LiteLLM n'a pas répondu. La clé est intacte, réessaie.":
    "The name could NOT be changed: LiteLLM did not respond. The key is intact — try again.",
  "Le nom doit contenir au moins un caractère (lettres, chiffres, - ou _).":
    "The name must contain at least one character (letters, digits, - or _).",
  // Model admin
  "Modèle introuvable.": "Model not found.",
  "Modèle arrêté.": "Model stopped.",
  "Échec de l'arrêt du modèle.": "Failed to stop the model.",
  "Lancement refusé par le runner.": "Launch refused by the runner.",
  "Nom et HF model ID requis.": "Name and HF model ID required.",
  "Un modèle avec ce nom existe déjà.": "A model with this name already exists.",
  "Durée invalide (ex: 1d, 7d, 30d, 12h).": "Invalid duration (e.g. 1d, 7d, 30d, 12h).",
  "Lancement de {name} accepté — chargement en cours.":
    "Launch of {name} accepted — loading in progress.",
  "Modèle introuvable dans le catalogue.": "Model not found in the catalogue.",
  "Modèle voix introuvable.": "Voice model not found.",
  "Un modèle OCR avec ce nom existe déjà.": "An OCR model with this name already exists.",
  "Un modèle voix avec ce nom existe déjà.": "A voice model with this name already exists.",
  "Nom et variante requis (variante hors liste autorisée).":
    "Name and variant required (variant outside the allowed list).",
  "Modèle OCR supprimé du catalogue.": "OCR model removed from the catalogue.",
  "Modèle voix supprimé du catalogue.": "Voice model removed from the catalogue.",
  "Args du modèle mis à jour.": "Model args updated.",
  "Args du modèle mis à jour (routage LiteLLM rafraîchi).":
    "Model args updated (LiteLLM routing refreshed).",
  // Budget / model requests
  "Montant irréaliste (> 1e12 tokens) — vérifie la saisie.":
    "Unrealistic amount (> 1e12 tokens) — check what you typed.",
  "Budget : nombre strictement positif attendu (tokens). Pour couper l'accès d'un compte, utilise le blocage du compte — réversible et tracé ; un budget de 0 le plafonne réellement à zéro token.":
    "Budget: a strictly positive number is expected (tokens). To cut an account's access, use account blocking — reversible and logged; a budget of 0 really caps it at zero tokens.",
  "Demande introuvable ou déjà traitée.": "Request not found or already handled.",
  "Demande introuvable.": "Request not found.",
  "Demande mise à jour.": "Request updated.",
  "Demande rejetée.": "Request rejected.",
  "Statut invalide.": "Invalid status.",
  "Tu as déjà une demande en attente.": "You already have a pending request.",
  "Demande envoyée pour « {name} » !": "Request sent for “{name}”!",
  "Le montant à ajouter doit être un nombre positif.": "The amount to add must be a positive number.",
  "Erreur lors de la mise à jour du budget sur LiteLLM.": "Failed to update the budget on LiteLLM.",
  "Durée de la subvention : nombre de jours entre 1 et 365 (vide = permanent).":
    "Grant duration: number of days between 1 and 365 (empty = permanent).",
  // Conversations
  "Action inconnue : {action}": "Unknown action: {action}",
  "Action inconnue.": "Unknown action.",
  "id manquant": "missing id",
  "messages invalides": "invalid messages",
  "conversation introuvable": "conversation not found",
  "conversation illisible": "unreadable conversation",
  // Generic admin / account
  "Tu ne peux pas supprimer ton propre compte.": "You cannot delete your own account.",
  "Tu ne peux pas purger ton propre compte.": "You cannot purge your own account.",
  "Tu ne peux pas bloquer ton propre compte.": "You cannot block your own account.",
  "Dernier administrateur local : nomme un autre administrateur avant de supprimer celui-ci.":
    "Last local administrator: appoint another administrator before deleting this one.",
  "Tu es le dernier administrateur local : nomme un autre administrateur avant de supprimer ton compte.":
    "You are the last local administrator: appoint another administrator before deleting your account.",
  "Ce compte est local : utilise la suppression, qui retire aussi l'accès.":
    "This account is local: use deletion, which also removes access.",
  "Confirmation requise : cette suppression emporte les accès ET les données du compte.":
    "Confirmation required: this deletion takes the account's access AND data.",
  "Confirmation requise : cette opération efface définitivement les données du compte.":
    "Confirmation required: this operation permanently erases the account's data.",
  "Compte créé, mais son quota n'a PAS pu être appliqué sur LiteLLM : le compte est sans plafond tant que le budget n'est pas redéfini.":
    "Account created, but its quota could NOT be applied on LiteLLM: the account has no cap until the budget is set again.",
  "Mode maintenance activé.": "Maintenance mode enabled.",
  "Mode maintenance désactivé.": "Maintenance mode disabled.",
  "Nom d'utilisateur invalide.": "Invalid username.",
  "Avatar inconnu.": "Unknown avatar.",
  // ASR / dictation / media
  "Modèle non chargé.": "Model not loaded.",
  "Modèle de transcription non chargé.": "Transcription model not loaded.",
  "Fichier audio trop volumineux.": "Audio file too large.",
  "Audio illisible.": "Unreadable audio.",
  "Format audio non supporté.": "Unsupported audio format.",
  "Enregistrement trop court.": "Recording too short.",
  "Enregistrement trop long ({dur}s, maximum {max}s).": "Recording too long ({dur}s, maximum {max}s).",
  "Enregistrement refusé.": "Recording refused.",
  "Trop de générations en cours. Attends la fin des précédentes.":
    "Too many generations in progress. Wait for the previous ones to finish.",
  "Trop de générations d'images en cours. Attends la fin des précédentes.":
    "Too many image generations in progress. Wait for the previous ones to finish.",
  "Ce job n'est plus actif.": "This job is no longer active.",
  "Trop de générations d'images en cours pour ce compte — attends la fin des précédentes.":
    "Too many image generations in progress for this account — wait for the previous ones to finish.",
  "La génération d'image a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.":
    "Image generation failed (sidecar unavailable or overloaded). Say it to the user as-is.",
  // Memory
  "Fait introuvable.": "Fact not found.",
  "Fichier trop volumineux.": "File too large.",
  "Aucun fait reconnu dans ce fichier.": "No fact recognised in this file.",
  "JSON invalide.": "Invalid JSON.",
  "Encodage invalide (UTF-8 attendu).": "Invalid encoding (UTF-8 expected).",
  "Modèle image inconnu.": "Unknown image model.",
  // Support: confirmation of a sensitive action + votes
  "Demande inconnue.": "Unknown request.",
  "Cette demande a expiré (ou a déjà été traitée).": "This request expired (or has already been handled).",
  "Cette demande a déjà été traitée.": "This request has already been handled.",
  "Vote invalide.": "Invalid vote.",
  // Remaining displayed sentences (admin announcements, email config, model
  // request, webauthn, maintenance API)
  "Annonce publiée — elle s'affichera à l'ouverture du site.":
    "Announcement published — it will show up when the site opens.",
  "Titre requis pour l'annonce.": "A title is required for the announcement.",
  "Dernier administrateur local : nomme un autre administrateur avant de bloquer celui-ci.":
    "Last local administrator: appoint another administrator before blocking this one.",
  "Mode maintenance en cours — l'API est temporairement indisponible, réessaie plus tard.":
    "Maintenance in progress — the API is temporarily unavailable, try again later.",
  "SMTP non configuré (renseigne-le dans Admin → Config, ou SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ADMIN_EMAIL).":
    "SMTP not configured (set it in Admin → Config, or SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ADMIN_EMAIL).",
  "Catégorie inconnue.": "Unknown category.",
  "Champs manquants.": "Missing fields.",
  // ── Playground: targeted file edits (server edit tools) ──
  "lecture de {f}": "reading {f}",
  "modification de {f}": "editing {f}",
  "modification de {f} non appliquée : {e}": "edit of {f} not applied: {e}",
  "texte introuvable": "text not found",
  "texte présent plusieurs fois": "text found several times",
  "fichier inconnu": "unknown file",
  "arguments invalides": "invalid arguments",
  "fichier trop volumineux": "file too large",
  "Aucune différence.": "No difference.",
  "création": "create",
  "modification": "edit",
  // ── Skills (SKILL.md packages, Claude Code format) ──
  "Compétence « {name} » jointe au prochain message": "Skill “{name}” attached to the next message",
  "Compétence synchronisée.": "Skill synchronized.",
  "La synchronisation a échoué.": "Synchronization failed.",
  "fichier(s)": "file(s)",
  "Contenu": "Content",
  "synchronisée le": "synchronized on",
  "Synchroniser": "Synchronize",
  "Télécharger (.zip)": "Download (.zip)",
  "Importer une compétence (.zip)": "Import a skill (.zip)",
  "Importer depuis un dépôt Git": "Import from a Git repository",
  "Un dossier contenant un SKILL.md, ou un plugin / dépôt qui en contient plusieurs.": "A folder holding a SKILL.md, or a plugin / repository holding several.",
  "GitHub, GitLab, Gitea/Codeberg ou une adresse .zip. Dépôts publics uniquement.": "GitHub, GitLab, Gitea/Codeberg or a .zip address. Public repositories only.",
  "Archive .zip": ".zip archive",
  "Adresse du dépôt ou du dossier": "Repository or folder address",
  "Branche": "Branch",
  "Dossier": "Folder",
  "Remplacer les compétences du même nom": "Replace skills with the same name",
  "L'import a échoué.": "The import failed.",
  "{n} compétence(s) importée(s) : {names}": "{n} skill(s) imported: {names}",
  "Ignorées :": "Skipped:",
  "Des compétences au format Claude Code (SKILL.md + fichiers). L'assistant et le playground les chargent quand elles correspondent à ta demande ; tape « / » pour en appeler une.": "Skills in the Claude Code format (SKILL.md + files). The assistant and the playground load them when they match your request; type “/” to call one.",
  "Importer (.zip)": "Import (.zip)",
  "Depuis Git": "From Git",
  "Crée une compétence, importe un .zip ou un dépôt Git (par exemple github.com/anthropics/skills).": "Create a skill, import a .zip or a Git repository (for example github.com/anthropics/skills).",
  "Activée": "Enabled",
  "compétence introuvable : {s}": "skill not found: {s}",
  "compétence chargée : {s}": "skill loaded: {s}",
  "Écrite ici": "Written here",
  "Importée (.zip)": "Imported (.zip)",
  "Dépôt Git": "Git repository",
  // ── Memory settings (Claude-style) ──
  "Le réglage n'a pas été enregistré.": "The setting was not saved.",
  "Rechercher et consulter les conversations": "Search and reference chats",
  "Autoriser l'assistant à rechercher des détails pertinents dans tes conversations précédentes.": "Allow the assistant to search for relevant details in your past conversations.",
  "Générer la mémoire à partir des conversations": "Generate memory from chat history",
  "Autoriser l'assistant à retenir des informations durables tirées de tes conversations, pour les réutiliser plus tard.": "Allow the assistant to remember lasting information from your conversations, to reuse it later.",
  "Inclure les sujets sensibles dans la mémoire": "Include sensitive topics in memory",
  "Autoriser l'assistant à enregistrer des détails sur des sujets sensibles, comme la santé ou les croyances religieuses.": "Allow the assistant to store details about sensitive topics, such as health or religious beliefs.",
  "Importer la mémoire depuis d'autres fournisseurs d'IA": "Import memory from other AI providers",
  "Récupère le contexte qu'un autre assistant (ChatGPT, Claude, Gemini…) a retenu sur toi : tu lui envoies une invite, tu colles sa réponse ici.": "Bring over the context another assistant (ChatGPT, Claude, Gemini…) has kept about you: send it a prompt, paste its answer here.",
  "Démarrer l'importation": "Start import",
  "Importer la mémoire d'un autre assistant": "Import memory from another assistant",
  "1. Copie cette invite dans ton autre assistant": "1. Copy this prompt into your other assistant",
  "2. Colle sa réponse ici": "2. Paste its answer here",
  "Réponse de l'autre assistant": "The other assistant's answer",
  "Les informations sont fusionnées avec ta mémoire actuelle ; les doublons sont ignorés.": "The information is merged with your current memory; duplicates are skipped.",
  "recherche dans tes conversations : {q}": "searching your conversations: {q}",
  "Les pièces jointes ne sont pas prises en charge : cette route est réservée au texte. Retire le fichier pour envoyer la demande.":
    "Attachments are not supported: this route is text-only. Remove the file to send the request.",
};
