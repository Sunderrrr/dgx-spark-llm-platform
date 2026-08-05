#!/bin/bash
# dgx-portal envoie un échantillon de référence FRAIS à /upload_reference à
# chaque génération (nom aléatoire, jamais réutilisé — voir voice_clone() dans
# app.py) et Chatterbox n'expose aucune route de suppression : sans ce
# nettoyage, /app/reference_audio grossit indéfiniment (jusqu'à 15 Mo par
# génération). Le fichier n'est utile que le temps de l'appel /tts qui suit
# immédiatement l'upload, donc une TTL d'une heure est très large.
#
# Tourne dans le conteneur lui-même plutôt que côté portail : celui-ci est
# non-root et n'a aucun accès au système de fichiers de ce conteneur.
set -euo pipefail

cleanup_loop() {
  while true; do
    find /app/reference_audio -maxdepth 1 -type f -mmin +60 -delete 2>/dev/null || true
    sleep 600
  done
}

cleanup_loop &

exec python3 server.py
