FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Utilisateur non-root (uid fixe pour matcher les permissions du volume /app/data).
RUN mkdir -p /app/data \
 && groupadd -r portal && useradd -r -u 10001 -g portal portal \
 && chown -R portal:portal /app
USER portal
EXPOSE 5000
# 4 workers × 16 threads = 64 requêtes simultanées (contre 32 avant le 04/08).
# Ce n'est pas du confort : chaque flux SSE monopolise un thread tant qu'il est
# ouvert — la page Admin garde un EventSource permanent vers /admin/runner/stream,
# et chaque Playground/Support en cours en tient un autre. Sous 32 créneaux,
# une charge de test soutenue (nombreux onglets/scripts en parallèle) a de
# nouveau saturé le pool le 04/08 : les requêtes suivantes étaient
# réinitialisées côté frontend (« socket hang up » sur /api/ocr/extract,
# reproductible même avec une petite image). Host à 20 CPU, large marge pour
# 4 workers gthread (I/O-bound, pas CPU-bound).
# --timeout 200 : une génération longue mais bien vivante ne doit pas être
# prise pour un worker bloqué. Relevé de 120 à 200 le 04/08 — l'OCR (timeout
# interne 180s, voir ocr_extract()) peut approcher 120s sous contention GPU
# (vidéo H3 en cours en parallèle), ce qui faisait tuer le worker en plein
# milieu d'une requête par ailleurs saine.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "4", "--worker-class", "gthread", "--threads", "16", "--timeout", "200", "app:app"]
