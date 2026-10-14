FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Non-root user (fixed uid to match the permissions of the /app/data volume).
# `/home/portal` is created because `useradd -r` does not, and gunicorn 26 puts
# its « control server » there (`$HOME/.gunicorn/gunicorn.ctl`): without that
# folder, every start writes `[ERROR] Control server error: [Errno 13] …` to the
# logs. The application serves anyway (verified), but a startup error must not
# become routine noise.
RUN mkdir -p /app/data /home/portal \
 && groupadd -r portal && useradd -r -u 10001 -g portal portal \
 && chown -R portal:portal /app /home/portal
USER portal
EXPOSE 5000
# 4 workers × 16 threads = 64 concurrent requests (against 32 before 08/04). Not
# a comfort: each SSE stream holds a thread while it is open — the Admin page
# keeps a permanent EventSource on /admin/runner/stream, and each ongoing
# Playground/Support holds another. With 32 slots, a sustained test load (many
# tabs/scripts in parallel) saturated the pool again on 08/04: the following
# requests were reset on the frontend side (« socket hang up » on
# /api/ocr/extract, reproducible even with a small image). The host has 20 CPUs,
# plenty of margin for 4 gthread workers (I/O-bound, not CPU-bound).
#
# --timeout 200: a long but very much alive generation must not be taken for a
# stuck worker. Raised from 120 to 200 on 08/04 — OCR (internal timeout 180 s,
# see ocr_extract()) can approach 120 s under GPU contention (H3 video running
# in parallel), which got the worker killed in the middle of an otherwise
# healthy request.
# --keep-alive 75: gunicorn's default (2 s) closes an idle connection exactly
# when the Next.js proxy reuses it — the proxy then fails with « socket hang
# up » (ECONNRESET, reproduced 2026-10-14 on bursts: 2 failures in 280). The
# server must hold idle connections LONGER than the client's pool does.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "4", "--worker-class", "gthread", "--threads", "16", "--timeout", "200", "--keep-alive", "75", "app:app"]
