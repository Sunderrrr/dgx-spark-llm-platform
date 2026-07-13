# French → English UI catalog.
#
# The portal templates are written in French (the language the maintainer runs).
# When PORTAL_LANG != "fr" (default "en"), app.py post-processes the rendered HTML
# and replaces these French phrases with English — so a fresh install reads in
# English while the French deployment (PORTAL_LANG=fr) is served untouched.
#
# Keys are applied longest-first, so a longer phrase wins over its substrings.

FR_TO_EN = {
    # ── Nav / layout (base.html) ──────────────────────────────────────────
    "Accueil": "Home",
    "Mes clés API": "My API keys",
    "Chercher un modèle": "Find a model",
    "Demander un modèle": "Request a model",
    "Classement": "Leaderboard",
    "Déconnexion": "Log out",
    "Basculer clair / sombre": "Toggle light / dark",
    "Basculer le thème": "Toggle theme",
    "Ton budget est utilisé à": "Your budget is",
    "il te reste": "used —",
    "tokens.": "tokens left.",
    "Demander plus": "Request more",
    "ou attends le reset quotidien.": "or wait for the daily reset.",

    # ── Carré « Nouveautés » (base.html) ──────────────────────────────────
    "Nouveautés": "What's new",
    "Compris": "Got it",
    "Nouveau modèle": "New model",
    "Changement de modèle": "Model change",
    "Un nouveau modèle est disponible": "A new model is available",
    "Le serveur ne peut faire tourner qu'un seul modèle à la fois.": "The server can only run one model at a time.",
    "Modèle actif :": "Active model:",
    "Il remplace": "It replaces",
    "Publier une annonce (affichée à l'ouverture du site)": "Publish an announcement (shown when the site opens)",
    "Titre (ex: Nouvelle fonctionnalité)": "Title (e.g. New feature)",
    "Détails (optionnel)": "Details (optional)",
    "Publier": "Publish",
    "Annonce publiée — elle s'affichera à l'ouverture du site.": "Announcement published — it will show when the site opens.",

    # ── Login (login.html) ────────────────────────────────────────────────
    "Plateforme IA privée · NVIDIA DGX Spark": "Private AI platform · NVIDIA DGX Spark",
    "Se connecter avec le SSO Cronos": "Sign in with Cronos SSO",
    "ou avec un compte LLDAP": "or with an LLDAP account",
    "Connexion par identifiant": "Sign in with username",
    "Identifiant LLDAP": "LLDAP username",
    "Mot de passe": "Password",
    "Se connecter": "Sign in",
    "Identifiants incorrects.": "Invalid credentials.",
    "Trop de tentatives. Réessaie dans": "Too many attempts. Try again in",
    "min.": "min.",

    # ── Home (index.html) ─────────────────────────────────────────────────
    "Bonjour,": "Hello,",
    "Bonjour ": "Hello ",
    "Ton accès self-service à l'inférence LLM sur DGX Spark.": "Your self-service access to LLM inference on the DGX Spark.",
    "Modèles disponibles maintenant": "Models available now",
    "En ligne": "Online",
    "Créer une clé API": "Create an API key",
    "État du serveur": "Server status",
    "Modèle actif": "Active model",
    "Débit": "Throughput",
    "Sessions occupées": "Sessions in use",
    "En cours / file": "Running / queued",
    "Requêtes servies": "Requests served",
    "Mon utilisation": "My usage",
    "aujourd'hui": "today",
    "Tokens · 24 h": "Tokens · 24 h",
    "Pic horaire": "Peak hour",
    "Clés actives": "Active keys",
    "Go": "GB",
    "aucun": "none",
    "arrêté": "stopped",
    "en ligne": "online",

    # ── Keys (keys.html) ──────────────────────────────────────────────────
    "Des clés personnelles pour appeler les modèles via l'API compatible OpenAI.":
        "Personal keys to call the models through the OpenAI-compatible API.",
    "Nouvelle clé": "New key",
    "compatible OpenAI.": "OpenAI-compatible.",
    "Budget illimité (admin)": "Unlimited budget (admin)",
    "partagé par toutes tes clés": "shared across all your keys",
    "Budget du": "Budget of your",
    "compte": "account",
    "vrais tokens : prompt + généré": "real tokens: prompt + generated",
    "tokens restants": "tokens left",
    "reset": "resets",
    "Demande en attente": "Request pending",
    "Demander plus de tokens": "Request more tokens",
    "Raison (optionnel)": "Reason (optional)",
    "Envoyer la demande": "Send request",
    "Alias": "Alias",
    "Clé": "Key",
    "Dépensé": "Spent",
    "tokens": "tokens",
    "Copier": "Copy",
    "Afficher": "Show",
    "Révoquer cette clé ?": "Revoke this key?",
    "Aucune clé pour l'instant.": "No key yet.",
    "en haut à droite pour en générer une.": "in the top-right to generate one.",
    "Intégrations": "Integrations",
    "Modèle": "Model",
    "Révéler la clé": "Reveal key",
    "Masquer la clé": "Hide key",
    "La clé est masquée par défaut dans les snippets": "The key is masked by default in the snippets",
    "Clé créée !": "Key created!",
    "Erreur lors de la création de la clé.": "Error creating the key.",
    "Clé introuvable.": "Key not found.",
    "Clé révoquée.": "Key revoked.",
    "Erreur lors de la révocation.": "Error revoking the key.",
    "Tu as déjà une demande en attente.": "You already have a pending request.",
    "Demande de tokens envoyée !": "Token request sent!",

    # ── Playground (playground.html) ──────────────────────────────────────
    "Teste le modèle directement dans le navigateur, sans configurer de client. Réponses en direct.":
        "Try the model right in your browser, no client setup. Live responses.",
    "aucun modèle actif": "no active model",
    "Nouvelle conversation": "New conversation",
    "Écris ton message…  (Entrée pour envoyer, Maj+Entrée = saut de ligne)":
        "Type your message…  (Enter to send, Shift+Enter = new line)",
    "Bac à sable gratuit. Un seul modèle tourne à la fois — bascule via l'admin si besoin.":
        "Free sandbox. Only one model runs at a time — switch via admin if needed.",
    "Aucun modèle actif.": "No active model.",
    "Erreur réseau.": "Network error.",
    "Règle les paramètres, joins des fichiers, réponses en direct.":
        "Tune the settings, attach files, live responses.",
    "— consomme ton budget.": "— uses your budget.",
    "Nouvelle conversation": "New conversation",
    "Historique": "History",
    "Aucune conversation.": "No conversation.",
    "Afficher le raisonnement": "Show reasoning",
    "Raisonnement": "Reasoning",
    "Analyser des logs": "Analyze logs",
    "Fichiers texte uniquement (pas de PDF/DOCX). Les tokens comptent sur ton budget.":
        "Text files only (no PDF/DOCX). Tokens count toward your budget.",
    "Éditer": "Edit",
    "Régénérer": "Regenerate",
    "Expliquer": "Explain",
    "Résumer": "Summarize",
    "Traduire": "Translate",
    "copier": "copy",
    "copié": "copied",
    "Rien à exporter.": "Nothing to export.",
    "tokens de contexte": "context tokens",
    "contexte presque plein": "context almost full",
    "⚠ contexte plein — nouvelle conversation": "⚠ context full — new conversation",

    # ── Support (support.html + app.py) ───────────────────────────────────
    "Un assistant IA connecté à la plateforme : il voit tes clés (masquées), ton budget et l'état du serveur pour t'aider en cas de pépin.":
        "An AI assistant wired into the platform: it sees your keys (masked), your budget and server status to help when something goes wrong.",
    "Je peux te dépanner": "I can troubleshoot",
    "mais aussi": "and also",
    "agir pour toi": "act for you",
    "créer une clé, demander du budget, demander un modèle. Dis-moi ce qu'il te faut.":
        "create a key, request budget, request a model. Tell me what you need.",
    "l'assistant de la plateforme.": "the platform assistant.",
    "Créer une clé": "Create a key",
    "Demander du budget": "Request budget",
    "Modèles dispo": "Available models",
    "Erreur 401": "401 error",
    "Quota 429": "429 quota",
    "Modèle KO": "Model down",
    "Config OpenCode": "OpenCode config",
    "Écris ton message…  (Entrée pour envoyer, Maj+Entrée pour un saut de ligne)":
        "Type your message…  (Enter to send, Shift+Enter for a new line)",
    "L'assistant ne voit que": "The assistant only sees",
    "tes": "your",
    "données (clés masquées). Ne colle jamais une clé complète ici.":
        "data (masked keys). Never paste a full key here.",
    "Pas de réponse.": "No response.",
    "Erreur réseau — réessaie.": "Network error — try again.",

    # ── Request a model (request_form.html) ───────────────────────────────
    "L'identifiant du modèle est requis.": "The model identifier is required.",
    "Tu as déjà une demande en attente pour ce modèle.": "You already have a pending request for this model.",

    # ── Ranking (ranking.html) ────────────────────────────────────────────
    "Qui consomme le plus": "Who consumes the most",
    "Jour": "Day",
    "Semaine": "Week",
    "Mois": "Month",
    "peu de données": "little data",
    "Aucune consommation sur cette période.": "No consumption in this period.",
    "nouveau": "new",

    # ── Admin (admin.html) ────────────────────────────────────────────────
    "Pilotage des modèles, quotas de tokens et demandes des utilisateurs.":
        "Manage models, token quotas and user requests.",
    "Modèles vLLM": "vLLM models",
    "Démarrage…": "Starting…",
    "Le processus s'est arrêté anormalement": "The process stopped unexpectedly",
    "vllm-runner inaccessible sur :8001": "vllm-runner unreachable on :8001",
    "Arrêté": "Stopped",
    "Aucun modèle actif": "No active model",
    "Arrêter le modèle ?": "Stop the model?",
    "Enregistrer les args": "Save args",
    "Remplacer le modèle actif ?": "Replace the active model?",
    "Supprimer ce modèle ?": "Delete this model?",
    "Ajouter un modèle": "Add a model",
    "Nom (ex: llama-3-8b)": "Name (e.g. llama-3-8b)",
    "Ajouter": "Add",
    "Logs vLLM": "vLLM logs",
    "Auto-scroll": "Auto-scroll",
    "Vue d'ensemble": "Overview",
    "Demandes en attente": "Pending requests",
    "Lancées": "Launched",
    "Refusées": "Rejected",
    "Limite de tokens par défaut": "Default token limit",
    "(nouvelles clés)": "(new keys)",
    "Tokens générés": "Generated tokens",
    "Durée (ex: 1d, 7d, 12h)": "Duration (e.g. 1d, 7d, 12h)",
    "Appliquer": "Apply",
    "S'applique aux clés créées après ce changement — n'affecte pas les clés existantes.":
        "Applies to keys created after this change — does not affect existing keys.",
    "Unité = vrais tokens : 1 token de prompt et 1 token généré comptent chacun pour 1.":
        "Unit = real tokens: 1 prompt token and 1 generated token each count as 1.",
    "Demandes de tokens": "Token requests",
    "Budget actuel": "Current budget",
    "Aucune demande.": "No request.",
    "Consommation par utilisateur": "Consumption per user",
    "Consommé aujourd'hui": "Used today",
    "Budget / jour": "Budget / day",
    "Demandes de modèles": "Model requests",
    "Lancé": "Launched",
    "Refusé": "Rejected",
    "Clés": "Keys",
    "Lancer": "Launch",
    "Arrêter": "Stop",
    "runner déconnecté": "runner disconnected",
    "Aucune clé créée.": "No key created.",
    "Statut invalide.": "Invalid status.",
    "Modèle introuvable.": "Model not found.",
    "Runner vLLM inaccessible.": "vLLM runner unreachable.",
    "Modèle arrêté.": "Model stopped.",

    # ── Home quick-action cards (index.html) ──────────────────────────────
    "Crée des clés personnelles pour accéder aux modèles via l'API OpenAI-compatible.":
        "Create personal keys to access the models through the OpenAI-compatible API.",
    "Limite :": "Limit:",
    "Illimitée (admin)": "Unlimited (admin)",
    "par clé.": "per key.",
    "Gérer mes clés": "Manage my keys",
    "Catalogue HuggingFace": "HuggingFace catalog",
    "Parcours les modèles disponibles sur HuggingFace et demande le lancement de celui qui t'intéresse.":
        "Browse the models available on HuggingFace and request the launch of the one you want.",
    "Explorer les modèles": "Browse models",
    "Tu connais un modèle que tu veux tester ? Envoie une demande à l'admin.":
        "Know a model you want to try? Send a request to the admin.",
    "Faire une demande": "Make a request",
    "Mes dernières demandes": "My latest requests",

    # ── Request a model (request_form.html) ───────────────────────────────
    "L'admin est notifié par Discord et email. Le statut apparaît sur ta page d'accueil.":
        "The admin is notified via Discord and email. The status shows on your home page.",
    "Identifiant HuggingFace": "HuggingFace identifier",
    "Format :": "Format:",
    "organisation/nom-du-modèle": "organization/model-name",
    "Pourquoi ce modèle ?": "Why this model?",
    "(optionnel)": "(optional)",
    "Ex: tester les capacités de raisonnement, comparer avec Ornith...":
        "e.g. test reasoning ability, compare with Ornith...",
    "Tu ne connais pas l'ID exact ?": "Don't know the exact ID?",
    "Cherche sur HuggingFace →": "Search on HuggingFace →",

    # ── Search (search.html) ──────────────────────────────────────────────
    "Explore le catalogue Hugging Face et demande le lancement d'un modèle sur le DGX.":
        "Browse the Hugging Face catalog and request a model launch on the DGX.",
    "Nom de modèle, ex: Qwen, Llama, Mistral...": "Model name, e.g. Qwen, Llama, Mistral...",
    "Chercher": "Search",
    "Tape un nom de modèle (Qwen, Llama, Mistral…) pour explorer Hugging Face.":
        "Type a model name (Qwen, Llama, Mistral…) to browse Hugging Face.",
    "Aucun résultat pour": "No result for",
    "Demander": "Request",

    # ── Ranking (ranking.html) ────────────────────────────────────────────
    "Qui consomme le plus, en tokens réellement consommés (prompt + généré).":
        "Who consumes the most, by tokens actually consumed (prompt + generated).",
    "comptes actifs": "active accounts",
    "compte actif": "active account",
    "Survole une ligne pour le détail prompt / généré.": "Hover a row for the prompt / generated breakdown.",
    "Total = tokens prompt + générés.": "Total = prompt + generated tokens.",
    "Delta vs": "Delta vs",
    "tokens au total": "tokens total",
    "tokens prompt": "prompt tokens",
    "générés": "generated",
    "généré": "generated",
    "coût pondéré": "weighted cost",
    "Coût pondéré": "Weighted cost",

    # ── Common ────────────────────────────────────────────────────────────
    "Erreur": "Error",
    "Annuler": "Cancel",
    "Enregistrer": "Save",
    "Supprimer": "Delete",
    "Valider": "Approve",
    "Rejeter": "Reject",
    "En attente": "Pending",
    "Validé": "Approved",
    "Rejeté": "Rejected",
    "Raison": "Reason",
    "Date": "Date",
    "Statut": "Status",
    "Utilisateur": "User",
    "Admin": "Admin",
}
