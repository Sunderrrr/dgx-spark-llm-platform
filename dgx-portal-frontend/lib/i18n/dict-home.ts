/**
 * English dictionary — home page and activity cards.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictHome: Record<string, string> = {
  // — Home —
  "Bonjour": "Hello",
  "Bonjour, comment allez-vous ?": "Hello, how are you?",
  "Bonsoir, comment allez-vous ?": "Good evening, how are you?",
  "Écris ton message ci-dessous pour commencer.": "Write your message below to get started.",
  "Ton accès self-service à l'inférence LLM sur DGX Spark.":
    "Your self-service access to LLM inference on the DGX Spark.",
  "Mes clés API": "My API keys",
  "Modèles disponibles maintenant": "Models available now",
  "Aucun modèle actif": "No active model",
  "En ligne": "Online",
  "Créer une clé API": "Create an API key",
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
  "Personne n'utilise le modèle en ce moment.":
    "No one is using the model right now.",
  "Mon utilisation — aujourd'hui": "My usage — today",
  "Tokens · 24 h": "Tokens · 24h",
  "Clés actives": "Active keys",
  "Limite :": "Limit:",
  "Illimitée (admin)": "Unlimited (admin)",
  "Catalogue HuggingFace": "HuggingFace catalog",
  "Parcours les modèles disponibles et demande le lancement de celui qui t'intéresse.":
    "Browse available models and request the one you're interested in.",
  "Explorer les modèles": "Explore models",
  "Mes dernières demandes": "My recent requests",
  "Modèle": "Model",
  "Raison": "Reason",
  "Statut": "Status",
  "Date": "Date",
  "En attente": "Pending",
  "Retirer": "Remove",
  "Mémoire": "Memory",
  "Désactivée par défaut. Une fois activée, l'assistant peut retenir des informations durables te concernant pour les réutiliser plus tard.":
    "Off by default. Once enabled, the assistant can keep durable information about you and reuse it later.",
  "Ajouter une information": "Add an entry",
  "Sujet": "Subject",
  "Information": "Entry",
  "ex : vLLM": "e.g. vLLM",
  "ex : Je sers mes modèles avec vLLM 0.27.": "e.g. I serve my models with vLLM 0.27.",
  "Ce qui est mémorisé": "What is remembered",
  "Capacité utilisée": "Capacity used",
  "informations": "entries",
  "Tout effacer": "Erase everything",
  "Rien de mémorisé pour l'instant": "Nothing remembered yet",
  "Les informations apparaîtront ici au fil des conversations.":
    "Entries will appear here as you chat.",
  "Active la mémoire ci-dessus, ou ajoute une information à la main.":
    "Turn memory on above, or add an entry by hand.",
  "Oublier": "Forget",
  "ajouté par toi": "added by you",
  "appris en conversation": "learned in conversation",
  "Mémoire activée.": "Memory enabled.",
  "Mémoire désactivée. Ce qui est déjà mémorisé est conservé.":
    "Memory disabled. What is already remembered is kept.",
  "Mémoire effacée": "Memory erased",
  "Ajout impossible.": "Could not add.",
  "Chargement impossible.": "Could not load.",
  "Chargement…": "Loading…",
  "Personne d'autre ne peut lire ta mémoire, administrateurs compris. Désactiver la collecte n'efface rien : utilise « Tout effacer » pour ça.":
    "Nobody else can read your memory, administrators included. Turning collection off erases nothing — use “Erase everything” for that.",
  "Exporter / importer": "Export / import",
  "Garde une copie de ce que l'assistant sait de toi (JSON pour réimporter ailleurs, Markdown pour le lire), ou restaure une mémoire exportée — JSON ou Markdown.":
    "Keep a copy of what the assistant knows about you (JSON to re-import elsewhere, Markdown to read), or restore an exported memory — JSON or Markdown.",
  "Exporter (JSON)": "Export (JSON)",
  "Exporter (Markdown)": "Export (Markdown)",
  "Importer": "Import",
  "Export impossible.": "Could not export.",
  "Import terminé": "Import complete",
  "Import impossible.": "Could not import.",
  "Fichier invalide.": "Invalid file.",
  "Modification impossible.": "Could not update.",
  "Le bouton copier donne toujours la vraie clé, même affichée masquée.":
    "The copy button always gives the real key, even when it is shown masked.",
  "Aucune clé API": "No API key",
  "Le playground consomme le budget de ton compte via ta clé API. Crée-en une pour pouvoir discuter avec le modèle.":
    "The playground spends your account budget through your API key. Create one to be able to chat with the model.",
  "Plusieurs réponses possibles.": "You can pick several answers.",
  "Bienvenue": "Welcome",
  "Trois minutes pour savoir quoi faire de cette plateforme.":
    "Three minutes to know what this platform can do for you.",
  "Étape": "Step",
  "Passer": "Skip",
  "C'est parti": "Let's go",
  "1. Crée ta clé API": "1. Create your API key",
  "Tout passe par elle : le Playground comme tes outils externes. Elle porte ton budget en tokens, elle est personnelle, et tu peux en créer plusieurs (une par machine, par exemple) pour en révoquer une sans casser les autres.":
    "Everything goes through it: the Playground and your own tools alike. It carries your token budget, it is personal, and you can create several (one per machine, say) so revoking one does not break the others.",
  "Ouvrir mes clés API": "Open my API keys",
  "2. Discute dans le Playground": "2. Chat in the Playground",
  "Le moyen le plus direct d'utiliser un modèle : rien à installer. Tu peux joindre des fichiers texte, suivre le débit en tokens/seconde, et le modèle te posera des questions s'il a besoin de précisions. Les fichiers qu'il écrit s'ouvrent dans un volet à côté.":
    "The most direct way to use a model — nothing to install. You can attach text files, watch the tokens per second, and the model will ask you questions when it needs details. Files it writes open in a panel beside the chat.",
  "Aller au Playground": "Go to the Playground",
  "3. Branche tes propres outils": "3. Plug in your own tools",
  "L'API est compatible OpenAI et Anthropic. Dans Réglages → Clés API, choisis ton outil (Claude Code, OpenCode, Cursor, Aider, cURL…) : la configuration est générée avec ta clé, prête à copier. Vise le modèle « auto-model » et tu n'auras rien à changer quand l'admin changera de modèle.":
    "The API speaks both OpenAI and Anthropic. In Settings → API keys, pick your tool (Claude Code, OpenCode, Cursor, Aider, cURL…): the configuration is generated with your key, ready to copy. Target the “auto-model” model and nothing needs changing when the admin switches models.",
  "Voir les intégrations": "See the integrations",
  "4. Besoin d'autre chose ?": "4. Need something else?",
  "Image, voix, OCR et vidéo ont chacun leur page. Si le modèle qu'il te faut n'est pas là, demande-le : un administrateur reçoit la demande et peut l'ajouter. Et le Support répond à tes questions sur la plateforme.":
    "Image, voice, OCR and video each have their own page. If the model you need is missing, request it: an admin gets the request and can add it. And Support answers your questions about the platform.",
  "Aperçu": "Preview",
  "Code source": "Source code",
  "Affichage": "Display",
  "Réponse coupée": "Reply cut short",
  "Le plafond de tokens a été atteint. Reprends la suite, ou augmente « Max tokens » dans les réglages.":
    "The token ceiling was reached. Pick up where it stopped, or raise “Max tokens” in the settings.",
  "Reprise automatique": "Auto-resume",
  "Le fichier s'arrête avant sa fin et les reprises automatiques n'ont pas suffi. Relance la suite, ou demande-lui de l'écrire en plusieurs fichiers.":
    "The file stops before its end and the automatic retries were not enough. Resume it again, or ask for it split across several files.",
  "La connexion s'est interrompue pendant la génération. Reprends la suite — ce qui est déjà écrit est conservé.":
    "The connection dropped mid-generation. Pick up where it stopped — what was already written is kept.",
  "Fichier inutilisable": "File will not run",
  "Le fichier se termine bien, mais son JavaScript ne compile pas — un bloc n'est jamais refermé, donc la page reste vide.":
    "The file ends properly, but its JavaScript does not compile — a block is never closed, so the page stays blank.",
  "Refaire le fichier": "Rebuild the file",
  "La page ne s'exécute pas": "The page does not run",
  "Corriger": "Fix it",
  "recherche « {q} »": "searching for “{q}”",
  "recherche impossible : {e}": "search failed: {e}",
  "{n} résultat(s) pour « {q} »": "{n} result(s) for “{q}”",
  "lecture de {n} page(s)": "reading {n} page(s)",
  "lecture impossible : {e}": "reading failed: {e}",
  "{n} page(s) lue(s)": "{n} page(s) read",
  ", {n} inaccessible(s)": ", {n} unreachable",
  "génération de l'image « {q} »": "generating the image “{q}”",
  "génération impossible : {e}": "image generation failed: {e}",
  "{n} image(s) générée(s)": "{n} image(s) generated",
  "génération de la vidéo « {q} »": "generating the video “{q}”",
  "génération vidéo impossible : {e}": "video generation failed: {e}",
  "{n} vidéo(s) générée(s)": "{n} video(s) generated",
  "lecture du document": "reading the document",
  "{n} caractère(s) extraits": "{n} character(s) extracted",
  "Plein écran": "Fullscreen",
  "Modification non appliquée": "Edit not applied",
  "Le texte à remplacer n'a pas été retrouvé dans le fichier. Demande la correction en précisant l'endroit, ou demande le fichier complet.":
    "The text to replace was not found in the file. Ask again pointing at the spot, or ask for the whole file.",
  "Modification proposée.": "Edit proposed.",
  "La modification proposée n'a pas pu être lue. Redemande la correction, ou demande le fichier complet.":
    "The proposed edit could not be read. Ask for the fix again, or ask for the whole file.",
  "Seul un extrait a été renvoyé": "Only an excerpt was returned",
  "Le modèle a donné la partie corrigée, pas le fichier complet. La version précédente reste ouverte dans le volet.":
    "The model gave the corrected part, not the whole file. The previous version stays open in the panel.",
  "Demander le fichier complet": "Ask for the whole file",
  "Ouvrir": "Open",
  "conversations": "conversations",
  "Messages en attente": "Queued messages",
  "Envoi imminent…": "Sending now…",
  "Envoi automatique dès la fin de la réponse. « Envoyer » interrompt et passe à ce message.":
    "Sent automatically when the reply finishes. “Send” interrupts it and moves to this message.",
  "Lancé": "Launched",
  "Refusé": "Rejected",

  // — Home (continued) —
  "CPU": "CPU",
  "RAM": "RAM",
  "GPU": "GPU",
  "TTFT": "TTFT",
  "Go": "GB",
  "API :": "API:",
};
