FROM python:3.12-slim
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
# 2 workers × 16 threads = 32 requêtes simultanées (contre 8 auparavant).
# Ce n'est pas du confort : chaque flux SSE monopolise un thread tant qu'il est
# ouvert — la page Admin garde un EventSource permanent vers /admin/runner/stream,
# et chaque Playground/Support en cours en tient un autre. Avec 8 créneaux,
# quelques onglets ouverts suffisaient à saturer le pool : les requêtes
# suivantes étaient réinitialisées côté frontend (« socket hang up » sur
# /api/home), et la page semblait ne pas s'afficher tant qu'on ne rechargeait pas.
# --timeout 120 : une génération longue mais bien vivante ne doit pas être
# prise pour un worker bloqué.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "--worker-class", "gthread", "--threads", "16", "--timeout", "120", "app:app"]
