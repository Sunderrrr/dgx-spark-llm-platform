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
  "Vidéo": "Video",
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
  "Disponible depuis l'application, non exposé par l'API.": "Available from the app, not exposed via the API.",
  "Ouvrir l'OCR": "Open OCR",
  "Ouvrir la génération vidéo": "Open video generation",
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

  // — Accueil (suite) —
  "CPU": "CPU",
  "RAM": "RAM",
  "GPU": "GPU",
  "TTFT": "TTFT",
  "Go": "GB",
  "API :": "API:",

  // — Clés API —
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
  "Dépensé": "Spent",
  "tokens": "tokens",
  "Révoquer": "Revoke",
  "Intégrations": "Integrations",
  "Masquer la clé": "Hide key",
  "Révéler la clé": "Reveal key",
  "Clé créée !": "Key created!",
  "Clé révoquée.": "Key revoked.",
  "Demande de tokens envoyée !": "Token request sent!",

  // — Playground —
  "Discute en direct avec un modèle actif — réglages avancés, fichiers joints, réponses en streaming, sur ton budget de compte.":
    "Chat live with an active model — advanced settings, attachments, streaming replies, on your account budget.",
  "Rien à exporter.": "Nothing to export.",
  "Erreur réseau.": "Network error.",
  "Le modèle n'a renvoyé aucune réponse.": "The model returned no response.",
  "Écris ton message… (Entrée pour envoyer, Maj+Entrée = saut de ligne)":
    "Write your message… (Enter to send, Shift+Enter for a new line)",
  "Utilisation du contexte": "Context usage",
  "System prompt (optionnel)": "System prompt (optional)",
  "Ex : Tu es un assistant concis et technique.": "E.g. You are a concise, technical assistant.",
  "Température": "Temperature",
  "Max tokens": "Max tokens",
  "Top-p": "Top-p",
  "Afficher le raisonnement": "Show reasoning",
  "Code Python": "Python code",
  "Génère une fonction, un script ou un test": "Generate a function, a script or a test",
  "Écris une fonction Python qui vérifie si un nombre est premier.":
    "Write a Python function that checks whether a number is prime.",
  "Expliquer": "Explain",
  "Décompose un sujet technique simplement": "Break down a technical topic simply",
  "Explique la mémoire unifiée du DGX Spark en termes simples.":
    "Explain the DGX Spark's unified memory in simple terms.",
  "Analyser des logs": "Analyse logs",
  "Trouve la cause d'une erreur dans un extrait de logs":
    "Find the cause of an error in a log excerpt",
  "Analyse ces logs et trouve la cause de l'erreur : ":
    "Analyse these logs and find the cause of the error: ",
  "Résumer": "Summarise",
  "Condense un texte en points clés": "Condense a text into key points",
  "Résume ce texte en 3 points : ": "Summarise this text in 3 points: ",

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

  // — Chercher un modèle —
  "Explore le catalogue Hugging Face et demande le lancement d'un modèle sur le DGX.":
    "Browse the Hugging Face catalog and request that a model be launched on the DGX.",
  "Recherche": "Search",
  "Nom de modèle, ex: Qwen, Llama, Mistral...": "Model name, e.g. Qwen, Llama, Mistral...",
  "Tâche": "Task",
  "Chercher": "Search",
  "Charger plus": "Load more",
  "Tout Hugging Face": "All of Hugging Face",
  "Seuls les modèles testés sur DGX Spark / GB10 sont affichés. Décoche pour élargir à tout Hugging Face.":
    "Only models tested on DGX Spark / GB10 are shown. Uncheck to widen to all of Hugging Face.",
  "Recherche élargie à tout Hugging Face — ces modèles ne sont pas garantis de tourner sur le GB10.":
    "Search widened to all of Hugging Face — these models are not guaranteed to run on the GB10.",
  "Tape un nom de modèle pour explorer Hugging Face.": "Type a model name to explore Hugging Face.",
  "Demander": "Request",

  // — Demander un modèle —
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

  // — Classement —
  "Qui consomme le plus, en tokens réellement consommés (prompt + généré).":
    "Who consumes the most, in real tokens used (prompt + generated).",
  "Période": "Period",
  "Consommation": "Consumption",
  "Aucune consommation sur cette période.": "No consumption over this period.",

  // — Admin —
  "Administration": "Administration",
  "Pilotage des modèles, quotas de tokens et demandes des utilisateurs.":
    "Model control, token quotas and user requests.",
  "Accès réservé aux administrateurs": "Administrators only",
  "Ton compte n'a pas les droits nécessaires pour voir cette page.":
    "Your account does not have the rights to view this page.",
  "Retour à l'accueil": "Back to home",
  "Action effectuée.": "Action completed.",
  "Modèles vLLM": "vLLM models",
  "Démarrage…": "Starting…",
  "Erreur": "Error",
  "Runner inaccessible": "Runner unreachable",
  "Arrêté": "Stopped",
  "Arrêter": "Stop",
  "Démarrer": "Start",
  "Injoignable": "Unreachable",
  "OCR & Vidéo": "OCR & Video",
  "Catalogue OCR": "OCR catalog",
  "Ajouter un modèle OCR": "Add an OCR model",
  "Nom (ex: unlimited-ocr)": "Name (e.g. unlimited-ocr)",
  "Lancer": "Launch",
  "Ajouter un modèle": "Add a model",
  "Nom (ex: llama-3-8b)": "Name (e.g. llama-3-8b)",
  "HF ID": "HF ID",
  "Moteur": "Engine",
  "Args": "Args",
  "Args du moteur": "Engine args",
  "Publier une annonce": "Publish an announcement",
  "Titre": "Title",
  "Détails": "Details",
  "Détails (optionnel)": "Details (optional)",
  "Publier": "Publish",
  "Logs —": "Logs —",
  "aucun modèle": "no model",
  "Demandes en attente": "Pending requests",
  "Lancées": "Launched",
  "Refusées": "Rejected",
  "Limite de tokens par défaut (nouvelles clés)": "Default token limit (new keys)",
  "Tokens générés": "Generated tokens",
  "Durée (ex: 1d, 7d, 12h)": "Duration (e.g. 1d, 7d, 12h)",
  "Appliquer": "Apply",
  "Demandes de tokens": "Token requests",
  "Consommation par utilisateur": "Consumption per user",
  "Utilisation OCR par utilisateur": "OCR usage per user",
  "Ne passe pas par une clé API — jamais visible dans la conso LiteLLM ci-dessus.":
    "Does not go through an API key — never visible in the LiteLLM consumption above.",
  "Extractions": "Extractions",
  "Utilisation vidéo par utilisateur": "Video usage per user",
  "Générations": "Generations",
  "Dernière utilisation": "Last used",
  "Mode maintenance actif": "Maintenance mode active",
  "Mode maintenance": "Maintenance mode",
  "Bloque l'accès à l'API et au chat/OCR/vidéo pour les non-admins, sans arrêter les modèles. Les admins gardent l'accès.":
    "Blocks API and chat/OCR/video access for non-admins, without stopping the models. Admins keep access.",
  "Désactiver": "Disable",
  "Activer": "Enable",
  "Mode maintenance en cours": "Maintenance mode in progress",
  "L'accès à l'API et aux fonctionnalités du site est temporairement suspendu. Réessaie plus tard.":
    "Access to the API and site features is temporarily suspended. Try again later.",
  "Demandes de modèles": "Model requests",
  "Utilisateur": "User",
  "Budget actuel": "Current budget",
  "Budget / jour": "Budget / day",
  "Clés": "Keys",
  "Action": "Action",
  "Approuver": "Approve",
  "Refuser": "Reject",
  "Lancé ✓": "Launched ✓",
  "en attente": "pending",

  "Aucun résultat pour": "No results for",

  // — Connexion —
  "Plateforme IA privée · NVIDIA DGX Spark": "Private AI platform · NVIDIA DGX Spark",
  "Identifiant LLDAP": "LLDAP username",
  "Mot de passe": "Password",
  "Se connecter": "Sign in",
  "Ou": "Or",
  "Se connecter avec le SSO Cronos": "Sign in with Cronos SSO",

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

  // — OCR —
  "OCR injoignable.": "OCR unreachable.",
  "Copié.": "Copied.",
  "Aucun modèle OCR n'est disponible": "No OCR model is available",
  "Demande à un admin de démarrer un modèle OCR pour utiliser cette page.":
    "Ask an admin to start an OCR model to use this page.",
  "Extrait le texte d'une image ou d'un document scanné.":
    "Extracts text from an image or a scanned document.",
  "Aperçu du document": "Document preview",
  "Changer d'image": "Change image",
  "Image ou scan": "Image or scan",
  "PNG, JPEG ou WebP — 15 Mo max.": "PNG, JPEG or WebP — 15 MB max.",
  "Instruction (optionnel)": "Instruction (optional)",
  "Extraire le texte": "Extract text",
  "dernier": "recent",
  "derniers": "recent",
  "(vide)": "(empty)",
  "Extraction en cours…": "Extracting…",
  "Terminé": "Done",
  "Vue du résultat": "Result view",
  "Texte": "Text",
  "Zones détectées": "Detected zones",
  "détecté(e)": "detected",
  "Élément détecté": "Element detected",
  "Le résultat s'affichera ici": "The result will appear here",
  "Choisis une image à gauche puis lance l'extraction.":
    "Pick an image on the left, then start the extraction.",

  // — Vidéo —
  "Aucun modèle vidéo n'est disponible": "No video model is available",
  "Demande à un admin de démarrer un modèle vidéo pour utiliser cette page.":
    "Ask an admin to start a video model to use this page.",
  "Génération vidéo — MiniMax H3": "Video generation — MiniMax H3",
  "Une description, avec ou sans image de référence, → une courte vidéo avec audio synchronisé. Génère localement sur le GPU, compte 5 à 10 minutes selon la charge.":
    "A description, with or without a reference image, → a short video with synced audio. Generates locally on the GPU, allow 5 to 10 minutes depending on load.",
  "Image de référence (optionnel)": "Reference image (optional)",
  "PNG, JPEG ou WebP — 15 Mo max. Sans image, génère depuis le texte seul.":
    "PNG, JPEG or WebP — 15 MB max. Without an image, generates from text alone.",
  "Décris la scène": "Describe the scene",
  "Ex : un ballon rouge qui rebondit sur un sol blanc, caméra fixe.":
    "E.g. a red ball bouncing on a white floor, static camera.",
  "Durée": "Duration",
  "Générer": "Generate",
  "En file d'attente…": "Queued…",
  "Génération en cours…": "Generating…",
  "Vidéo prête.": "Video ready.",
  "Échec de la génération.": "Generation failed.",
  "Progression": "Progress",
  "Historique (3 dernières)": "History (3 most recent)",
  "ComfyUI injoignable.": "ComfyUI unreachable.",

  // — Connexion (suite) —
  "Session expirée — recharge la page et réessaie.": "Session expired — reload the page and try again.",
  "Identifiants incorrects.": "Incorrect credentials.",

  // — Réglages : MCP / Compétences (suite) —
  "Serveur MCP mis à jour": "MCP server updated",
  "Serveur MCP connecté": "MCP server connected",
  "outil(s) trouvé(s)": "tool(s) found",
  "Serveur activé": "Server enabled",
  "Exemple : notion_workspace": "Example: notion_workspace",
  "Exemple : analyse-de-logs": "Example: log-analysis",

  // — Playground (suite) —
  "Vous": "You",

  // — Classement (suite) —
  "toi": "you",

  // — Accueil / activité (suite) —
  "Moins → Plus": "Less → More",

  // — Réglages : Usage (suite) —
  "Maximum 20 messages par minute.": "Maximum 20 messages per minute.",
  "% utilisé": "% used",

  // — Support (suite) —
  "Bonjour 👋 Je suis **Cronos**, l'assistant de la plateforme. Je peux te dépanner (clé, quota, modèle, intégration OpenCode/Hermes…) mais aussi **agir pour toi** : créer une clé, demander du budget, demander un modèle. Dis-moi ce qu'il te faut.":
    "Hi 👋 I'm **Cronos**, the platform's assistant. I can help you troubleshoot (keys, quota, models, OpenCode/Hermes integration…) but I can also **act on your behalf**: create a key, request budget, request a model. Tell me what you need.",

  // — Étiquettes d'appels d'outils (assistant Support) —
  "Révoquer une clé API": "Revoke an API key",
  "Lancer un modèle": "Launch a model",
  "Arrêter le modèle": "Stop the model",
  "Action bloquée après lecture d'un contenu externe.": "Action blocked after reading external content.",

  // — Classement (suite 2) —
  "Jour": "Day",
  "Semaine": "Week",
  "Mois": "Month",
  "compte actif": "active account",
  "comptes actifs": "active accounts",
  "nouveau": "new",
  "Delta vs": "Delta vs",
  "Total = tokens prompt + générés.": "Total = prompt + generated tokens.",
  "Aujourd'hui": "Today",
  "7 derniers jours": "Last 7 days",
  "30 derniers jours": "Last 30 days",
  "hier": "yesterday",
  "la semaine précédente": "the previous week",
  "les 30 jours précédents": "the previous 30 days",

  // — Indicateur de réflexion —
  "Cogitation": "Cogitating",
  "Rumination": "Ruminating",
  "Gamberge": "Pondering",
  "Mijotage": "Simmering",
  "Élucubration": "Musing",
  "Ébullition": "Percolating",
  "Méditation": "Meditating",
  "Tergiversation": "Waffling",
  "Concoction": "Concocting",
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
