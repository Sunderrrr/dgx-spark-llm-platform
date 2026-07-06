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
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "--worker-class", "gthread", "--threads", "4", "--timeout", "60", "app:app"]
