/**
 * English dictionary — login page, dictation, OCR, video and voice cloning.
 *
 * Keys are the French UI strings (gettext msgids), values their English
 * translation. Entries were moved verbatim out of the former single-file
 * dictionary; do not rename a key, it is the msgid used by `t()`. Placeholders
 * (`{n}`, `{name}`, `{wait}`…) are part of the msgid and must stay as-is.
 */
export const dictMedia: Record<string, string> = {
  // — Login —
  "Plateforme IA privée · NVIDIA DGX Spark": "Private AI platform · NVIDIA DGX Spark",
  "Mot de passe": "Password",
  "Se connecter": "Sign in",
  "Ou": "Or",
  "Se connecter avec le SSO {name}": "Sign in with {name} SSO",

  // — Playground / Support —
  "Reprendre la conversation précédente": "Resume the previous conversation",
  "Elle n'est pas chargée : le Support démarre toujours à zéro.": "It is not loaded: Support always starts from scratch.",
  "Réflexion": "Thinking",
  "Réflexion en cours…": "Thinking…",
  "Réflexion du modèle (plus lente, plus coûteuse)": "Model thinking (slower, costlier)",
  "Auto (selon la demande)": "Auto (based on the request)",
  "Toujours": "Always",
  "Jamais": "Never",
  "Lecture du fichier…": "Reading the file…",
  "Nouvelle conversation": "New conversation",
  "Historique": "History",
  "Aucune conversation": "No conversations",
  "Joindre un fichier": "Attach a file",
  "Joindre un fichier ou une image": "Attach a file or an image",
  "« {name} » : ce modèle ne lit pas les images.": "\"{name}\": this model can't read images.",
  "4 images au plus par message.": "At most 4 images per message.",
  "« {name} » dépasse 25 Mo.": "\"{name}\" is larger than 25 MB.",
  "Ce modèle ne lit pas les images : elles n'ont pas été envoyées.": "This model can't read images: they were not sent.",
  "Image jointe {n}": "Attached image {n}",
  "« {name} » est trop grande (plus de 2 Mo) et ce navigateur bloque sa réduction : réduis-la avant de la joindre.": "\"{name}\" is too large (over 2 MB) and this browser blocks resizing it: shrink it before attaching.",
  "image": "image",
  "Fichiers joints": "Attached files",
  "Fichiers texte uniquement. Les tokens comptent sur ton budget.":
    "Text files only. Tokens count against your budget.",
  "Raisonnement": "Reasoning",
  "Dicter": "Dictate",
  "Arrêter la dictée": "Stop dictation",
  "Transcription…": "Transcribing…",
  "Aucune parole détectée.": "No speech detected.",
  "Échec de la transcription.": "Transcription failed.",

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
  "Élément détecté": "Element detected",
  "Le résultat s'affichera ici": "The result will appear here",
  "Choisis une image à gauche puis lance l'extraction.":
    "Pick an image on the left, then start the extraction.",

  // — Video —
  "Aucun modèle vidéo n'est disponible": "No video model is available",
  "Demande à un admin de démarrer un modèle vidéo pour utiliser cette page.":
    "Ask an admin to start a video model to use this page.",
  "Génération vidéo": "Video generation",
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
  "En cours": "In progress",
  "ComfyUI injoignable.": "ComfyUI unreachable.",

  // — Voice (cloning) —
  "Aucun modèle vocal n'est disponible": "No voice model is available",
  "Demande à un admin de démarrer un modèle vocal pour utiliser cette page.":
    "Ask an admin to start a voice model to use this page.",
  "Clonage de voix": "Voice cloning",
  "Un échantillon de ta voix, un texte, → le texte lu avec cette voix. Génère localement sur le GPU, quelques secondes suffisent.":
    "A sample of your voice, some text, → that text read in that voice. Generates locally on the GPU, a few seconds is enough.",
  "WAV ou MP3 — 15 Mo max, au moins quelques secondes de voix claire.":
    "WAV or MP3 — 15 MB max, at least a few seconds of clear speech.",
  "Trop court : enregistre au moins 6 secondes de voix.":
    "Too short: record at least 6 seconds of speech.",
  "Échantillon vocal de référence": "Reference voice sample",
  "Texte à lire": "Text to read",
  "Ex : Bonjour, ceci est un test de clonage vocal.":
    "E.g. Hello, this is a voice cloning test.",
  "Générer la voix": "Generate voice",
  "Voix prête.": "Voice ready.",
  "Service voix injoignable.": "Voice service unreachable.",
  "Ouvrir le clonage de voix": "Open voice cloning",
  "Backends": "Backends",
  "Catalogue": "Catalog",
  "Type de modèle": "Model type",
  "LLM": "LLM",
  "Nom (ex: qwen3-tts)": "Name (e.g. qwen3-tts)",
  "La vidéo n'a pas de catalogue : un seul workflow ComfyUI figé, démarré et arrêté depuis la ligne « Backends » ci-dessus.":
    "Video has no catalog: a single fixed ComfyUI workflow, started and stopped from the “Backends” row above.",
  "Ajouter un modèle voix": "Add a voice model",
  // Variants of the « Ajouter un modèle voix » selector (the repo_id value does
  // not change: only the label is translated).
  "Qwen3-TTS 1.7B (10 langues)": "Qwen3-TTS 1.7B (10 languages)",
  "Qwen3-TTS 0.6B (10 langues)": "Qwen3-TTS 0.6B (10 languages)",
  "Variante": "Variant",
  // "Enregistrer" alone is already taken by the "Save" meaning (Settings) — the
  // French text serving as the key, we need a distinct label here.
  "Langue du texte": "Text language",
  "Transcription de l'échantillon (optionnel)": "Sample transcript (optional)",
  "Recopie ici exactement ce que tu as dit dans l'enregistrement.":
    "Type here exactly what you said in the recording.",
  "Améliore nettement la ressemblance. Sans elle, seule l'empreinte vocale est utilisée.":
    "Noticeably improves likeness. Without it, only the voice fingerprint is used.",
  "Source de la voix": "Voice source",
  "Enregistrer au micro": "Record from mic",
  "Importer un fichier": "Upload a file",
  "Parle pendant 10 à 30 secondes pour un bon résultat — 1 minute maximum, l'enregistrement s'arrête tout seul.":
    "Speak for 10 to 30 seconds for a good result — 1 minute maximum, recording stops on its own.",
  "Démarrer l'enregistrement": "Start recording",
  "Arrêter l'enregistrement": "Stop recording",
  "Enregistrement en cours": "Recording",
  "Enregistrement": "Recording",
  "Réenregistrer": "Record again",
  "Micro inaccessible — autorise l'accès au microphone dans ton navigateur.":
    "Microphone unavailable — allow microphone access in your browser.",
  "Impossible de convertir l'enregistrement.": "Could not convert the recording.",
  // Messages returned by /api/voice/generate (the backend stays French-speaking,
  // the page displays them as-is via t()).
  "Échec de la génération — l'échantillon doit contenir plus de 5 secondes de voix.":
    "Generation failed — the sample must contain more than 5 seconds of speech.",
  "Échantillon audio refusé par le service voix.": "Audio sample rejected by the voice service.",
  "Échec de l'envoi de l'échantillon audio.": "Failed to upload the audio sample.",
  "Le service voix a mis trop de temps à répondre.": "The voice service took too long to respond.",
  "Un texte est requis.": "Some text is required.",
  "Aucun échantillon audio fourni.": "No audio sample provided.",
  "Format audio non supporté (WAV/MP3 uniquement).": "Unsupported audio format (WAV/MP3 only).",
  "Échantillon audio trop volumineux (15 Mo max).": "Audio sample too large (15 MB max).",
};
