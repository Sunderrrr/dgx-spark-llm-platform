/**
 * English dictionary — security and account: passkeys/2FA, sessions, admin blocking, Hugging Face search, API-key dates, budget and deletions.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictSecurity: Record<string, string> = {
  // ── Two-factor authentication with a security key (WebAuthn / passkeys) ──
  "Sécurité": "Security",
  "Exiger une clé de sécurité au login": "Require a security key at login",
  "Ta clé sera demandée après le mot de passe (local ou LDAP).":
    "Your key will be requested after the password (local or LDAP).",
  "Une fois activée, la clé est exigée à chaque connexion.":
    "Once enabled, the key is required on every sign-in.",
  "Clés de sécurité": "Security keys",
  "Ajouter une clé": "Add a key",
  "Clé de sécurité": "Security key",
  "Clé de sécurité ajoutée.": "Security key added.",
  "Échec de l'enregistrement de la clé.": "Failed to register the key.",
  "Ajout de clé annulé.": "Key addition cancelled.",
  "Ce navigateur ne supporte pas les clés de sécurité.":
    "This browser does not support security keys.",
  "Aucune clé de sécurité": "No security key",
  "Ajoute une passkey, une YubiKey ou une clé 1Password pour sécuriser ton compte.":
    "Add a passkey, a YubiKey, or a 1Password key to secure your account.",
  "Confirme avec ton mot de passe pour supprimer la clé.":
    "Confirm with your password to delete the key.",
  "Confirme avec ton mot de passe pour changer la double authentification.":
    "Confirm with your password to change two-factor authentication.",
  "Confirme avec ton mot de passe pour ajouter une clé.":
    "Confirm with your password to add a key.",
  "Confirmer": "Confirm",
  "Clé supprimée.": "Key deleted.",
  "Double authentification mise à jour.": "Two-factor authentication updated.",
  // ── 2FA step on the login side ──
  "Double authentification annulée.": "Two-factor authentication cancelled.",
  "Vérification de la clé échouée.": "Key verification failed.",
  // ── « demander un modèle » button on the media pages ──
  "Envie de ce modèle ? Préviens le responsable, il pourra le lancer.":
    "Want this model? Let the admin know, they can launch it.",
  "Demander ce modèle": "Request this model",
  "Demande envoyée": "Request sent",
  "Demande envoyée au responsable.": "Request sent to the admin.",
  "La demande a échoué.": "The request failed.",
  // ── Home: media service cards (« Ouvrir » button) ──
  "Ouvrir le chat": "Open chat",
  "Ouvrir la génération d'image": "Open image generation",
  // ── Support: dynamic home, confirmation of a sensitive action,
  //    stream stop, restored thread and votes ──
  "Pourquoi le modèle est-il arrêté ?": "Why is the model stopped?",
  "Diagnostique l'arrêt et propose de le relancer": "Diagnoses the outage and offers to relaunch it",
  "Pourquoi le modèle est-il arrêté ? Peux-tu le relancer ?": "Why is the model stopped? Can you relaunch it?",
  "Comment créer ma clé API ?": "How do I create my API key?",
  "L'assistant te la crée et t'explique comment l'utiliser": "The assistant creates it for you and explains how to use it",
  "Comment créer ma clé API ? Peux-tu le faire pour moi ?": "How do I create my API key? Can you do it for me?",
  "Cette action sensible n'a pas encore été exécutée : elle attend ta confirmation.":
    "This sensitive action has not run yet — it is waiting for your confirmation.",
  "Réponse interrompue.": "Response interrupted.",
  "Réponse utile": "Helpful answer",
  "Réponse pas utile": "Unhelpful answer",
  "Un commentaire ? (facultatif)": "A comment? (optional)",
  "Merci, c'est noté.": "Thanks, noted.",

  // ── Security: active sessions + password change ──
  "Double authentification, sessions actives et mot de passe de ton compte.":
    "Two-factor authentication, active sessions, and your account password.",
  "Sessions actives": "Active sessions",
  "Les appareils connectés à ton compte — révoque ceux que tu ne reconnais pas.":
    "Devices signed in to your account — revoke the ones you don't recognise.",
  "Révoquer les autres sessions": "Revoke other sessions",
  "Session révoquée.": "Session revoked.",
  "Sessions révoquées.": "Sessions revoked.",
  "Cet appareil": "This device",
  "Aucune session active": "No active sessions",
  "Les sessions apparaîtront ici.": "Sessions will appear here.",
  "Expire": "Expires",
  "Le mot de passe sert au login et à confirmer les actions sensibles.":
    "The password is used to sign in and to confirm sensitive actions.",
  "Mot de passe actuel": "Current password",
  "Confirmer le nouveau mot de passe": "Confirm new password",
  "Changer le mot de passe": "Change password",
  "Mot de passe modifié.": "Password changed.",
  "Les deux nouveaux mots de passe ne correspondent pas.": "The two new passwords don't match.",

  // ── Admin: blocking, account details, deletion ──
  "Bloquer…": "Block…",
  "Bloquer": "Block",
  "Débloquer": "Unblock",
  "Bloqué": "Blocked",
  "Bloquer ce compte": "Block this account",
  "Le compte sera refusé au login, quelle que soit la source d'authentification, et ses sessions actives seront révoquées.":
    "The account will be refused at sign-in whatever its authentication source, and its active sessions will be revoked.",
  "Compte bloqué.": "Account blocked.",
  "Compte bloqué — {n} session(s) révoquée(s).": "Account blocked — {n} session(s) revoked.",
  "Compte débloqué.": "Account unblocked.",
  "verrouillé après trop d'échecs, {n} min": "locked after too many failures, {n} min",
  "Dernières actions de ce compte": "Recent actions by this account",
  "Détails du compte": "Account details",
  "Dernière source": "Last source",
  "Dernière activité": "Last activity",
  "par": "by",
  "Surcharge utilisateur": "User override",
  "Quota effectif": "Effective quota",
  "Dépensé (LiteLLM)": "Spent (LiteLLM)",
  "Aucun profil LiteLLM": "No LiteLLM profile",
  "Fenêtre de budget": "Budget window",
  "Sans nom": "Unnamed",
  "Conversations": "Conversations",
  "Révoquer toutes ses sessions": "Revoke all sessions",
  "Supprimer ce compte": "Delete this account",
  "Cette action est définitive : elle révoque les clés API et les sessions du compte, supprime son enveloppe LiteLLM ET ses données personnelles (mémoires, conversations, préférences, passkeys).":
    "This action is permanent: it revokes the account's API keys and sessions, deletes its LiteLLM envelope AND its personal data (memories, conversations, preferences, passkeys).",
  "Tapez DELETE pour confirmer": "Type DELETE to confirm",
  "Supprimer définitivement": "Delete permanently",
  "Compte supprimé.": "Account deleted.",
  // Purge of a directory account (LDAP/SSO): data erased, access kept.
  "Purger les données…": "Purge data…",
  "Purger les données": "Purge data",
  "Purger": "Purge",
  "Données du compte purgées.": "Account data purged.",
  "Cette action efface DÉFINITIVEMENT les données du compte : mémoires, conversations, liens de partage, préférences et clés API.":
    "This action PERMANENTLY erases the account's data: memories, conversations, share links, preferences and API keys.",
  "Elle ne retire pas l'accès : un compte LDAP/SSO peut se reconnecter et repartira d'un compte vide. Pour retirer l'accès, utilise Bloquer.":
    "It does not remove access: an LDAP/SSO account can sign in again and will start from an empty account. To remove access instead, use Block.",

  // Admin: actions and confirmations ({ok, error} contract of the backend; the
  // sentences returned by the server are displayed as-is, without a key).
  "Accès refusé.": "Access denied.",
  "Arrêter le modèle servi ?": "Stop the served model?",
  "Le modèle va être coupé : toutes les générations en cours, pour tous les utilisateurs, seront interrompues.":
    "The model will be shut down: every in-flight generation, for every user, will be interrupted.",
  "Lancer {name} ?": "Launch {name}?",
  "Le modèle sera chargé en mémoire unifiée — le lancement peut prendre plusieurs minutes.":
    "The model will be loaded into unified memory — launching can take several minutes.",
  "Supprimer {name} ?": "Delete {name}?",
  "Le modèle sera retiré du catalogue et du routage LiteLLM, et ses fichiers seront effacés du disque (sauf s'ils servent à un autre modèle). Pour le retélécharger, il faudra le réinstaller. Un modèle en cours doit d'abord être arrêté.": "The model will be removed from the catalogue and from LiteLLM routing, and its files will be erased from disk (unless another model uses them). To use it again it must be reinstalled. A running model must be stopped first.",
  "Activer le mode maintenance ?": "Enable maintenance mode?",
  "Désactiver le mode maintenance ?": "Disable maintenance mode?",
  "Le trafic non-admin vers les routes de génération sera refusé ; les admins gardent l'accès.":
    "Non-admin traffic to the generation routes will be refused; admins keep access.",
  "Le trafic non-admin vers les routes de génération sera de nouveau accepté.":
    "Non-admin traffic to the generation routes will be accepted again.",
  "Modifier les args du moteur": "Edit engine args",
  "Seuls les args du moteur changent ; l'entrée reste identifiée par son nom et son HF ID.":
    "Only the engine args change; the entry keeps its name and HF ID.",

  // Admin: « État de la plateforme » card (GET /admin/platform).
  "État de la plateforme": "Platform status",
  "Relevé indisponible — impossible de lire l'état de la plateforme.": "Status unavailable — could not read the platform state.",
  "Vérifié à {time}": "Checked at {time}",
  "Maintenance": "Maintenance",
  "Disque": "Disk",
  "Espace disque faible": "Low disk space",
  "Espace disque suffisant": "Disk space OK",
  "{v} Go libre": "{v} GB free",
  "{v} Go sur {w} Go libres": "{v} GB of {w} GB free",
  "Sauvegarde": "Backup",
  "sauvegarde illisible": "backup unreadable",
  "Sauvegarde en retard": "Backup overdue",
  "Sauvegarde récente": "Recent backup",
  "il y a {h} h": "{h} h ago",
  "âge inconnu": "unknown age",
  "archives": "archives",
  "Moniteur": "Monitor",
  "état non lisible": "state not readable",
  "incident(s) actif(s)": "active incident(s)",
  "aucun incident": "no incidents",
  "Modèle servi": "Served model",
  "en service depuis {u}": "up for {u}",
  "état inconnu": "unknown state",
  "Portail": "Portal",
  "base {v} Mo": "DB {v} MB",
  "sessions actives": "active sessions",
  "comptes locaux": "local users",
  "clés actives": "active keys",

  // User search (Admin): generic example, never a real account.

  // Hugging Face model search. The tasks were written in English in the code:
  // they were therefore not translatable and stayed English even in French. They
  // are now French msgids like the rest.
  "Génération de texte": "Text generation",
  "Texte vers texte": "Text to text",
  "Plongements (embeddings)": "Embeddings",
  "Texte vers image": "Text to image",
  "Texte vers vidéo": "Text to video",
  "Image vers texte": "Image to text",
  "Accès restreint": "Gated access",
  "Recherche impossible.": "Search failed.",
  "Résultats indisponibles.": "Results unavailable.",
  // Upstream Hugging Face outage (`code: hf_indisponible`). The server message
  // carries the technical detail but stays French: the screen shows this
  // translated sentence instead, and the detail stays in the portal logs.
  "Hugging Face ne répond pas. Réessaie dans un instant.":
    "Hugging Face is not responding. Try again in a moment.",
  "Aucun modèle GB10 ne correspond, mais il y en a sur tout Hugging Face : décoche le filtre ci-dessus.":
    "No GB10-tagged model matches, but Hugging Face does have some — clear the filter above.",
  "Hugging Face ne connaît aucun modèle pour cette recherche.":
    "Hugging Face knows no model for this search.",
  "Tu as déjà une demande en attente pour ce modèle.":
    "You already have a pending request for this model.",
  "L'identifiant du modèle est requis.": "The model id is required.",
  "Partage impossible :": "Sharing failed:",

  // ── API keys: direct copy, dates, renaming ──
  "Copier la clé": "Copy key",
  "Clé « {alias} » copiée.": "Key “{alias}” copied.",
  "Créée le": "Created",
  "Jamais utilisée": "Never used",
  "Nouveau nom pour la clé « {alias} » :": "New name for key “{alias}”:",
  "Clé renommée.": "Key renamed.",

  // ── Budget: the period, not the day ──
  "Consommé sur la période": "Used this period",
  "Remis à zéro le {date}.": "Resets on {date}.",

  // ── Account deletion and password source ──
  "Supprimer mon compte": "Delete my account",
  "Supprimer définitivement mon compte": "Permanently delete my account",
  "Effacer mes données": "Erase my data",
  "Ton mot de passe": "Your password",
  "La suppression a échoué.": "Deletion failed.",
  "Ton compte est supprimé. Tu vas être déconnecté…":
    "Your account is deleted. You are being signed out…",
  "Cela supprime ton compte, tes clés API, tes conversations et ta mémoire. C'est définitif.":
    "This deletes your account, your API keys, your conversations, and your memory. It cannot be undone.",
  "Cela efface tes données du portail (clés, conversations, mémoire, préférences). Ton compte, lui, appartient à l'annuaire : il continue d'exister.":
    "This erases your portal data (keys, conversations, memory, preferences). The account itself belongs to the directory and keeps existing.",
  "Tes données du portail sont effacées. Ton compte, lui, existe toujours dans l'annuaire : demande à un administrateur de le bloquer si tu pars.":
    "Your portal data has been erased. The account itself still exists in the directory — ask an administrator to block it if you are leaving.",
  "Compte SSO : la double authentification se règle chez ton fournisseur d'identité, pas ici.":
    "SSO account: two-factor authentication is managed by your identity provider, not here.",
  "Compte LDAP : ton mot de passe est celui de l'annuaire. Le portail ne le stocke pas, il le vérifie auprès de l'annuaire — change-le depuis ton administrateur.":
    "LDAP account: your password is the directory's. The portal does not store it, it verifies it against the directory — change it through your administrator.",
  "Compte SSO : ton mot de passe appartient au fournisseur d'identité, qui seul peut le changer. Le portail n'en connaît aucun.":
    "SSO account: your password belongs to the identity provider, which alone can change it. The portal knows none.",
  "Le portail n'a pas de mot de passe pour ce compte : il se connecte par l'annuaire (LDAP ou SSO), où le mot de passe se change.":
    "The portal has no password for this account: it signs in through the directory (LDAP or SSO), where the password is changed.",

  // ── Conversation deletion refused by the server ──
  "Suppression impossible — la conversation est conservée.":
    "Could not delete — the conversation is kept.",
  "Suppression impossible.": "Could not delete.",
  "Image trop lourde : 15 Mo maximum.":
    "Image too large: 15 MB maximum.",
  // Inert buttons when the CSRF token could not be obtained (CsrfProvider
  // exhausted its two attempts): say so instead of doing nothing.
  "Session incomplète — recharge la page.":
    "Incomplete session — reload the page.",
};
