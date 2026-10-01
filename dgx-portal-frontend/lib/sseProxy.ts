const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

// Time to receive the response HEADERS. Every Flask SSE route emits a ":"
// comment as its very first action precisely so the headers leave
// immediately, well within this budget — but a route that forgets to (or a
// backend under heavy load) used to turn into a bogus 502 "Le serveur ne
// repond pas" while the generation was in fact running fine, which the user
// experienced as a random failure on large conversations. 45 s leaves room
// for a slow-but-alive backend; a truly dead one refuses the connection
// instantly and never waits this out. Seen in prod on 22/08.
const CONNECT_TIMEOUT_MS = 45_000;
const IDLE_TIMEOUT_MS = 60_000; // time with no byte received once the stream has started

function sseErrorFrame(text: string): string {
  const payload = JSON.stringify({ choices: [{ delta: { content: text } }] });
  return `data: ${payload}\n\ndata: [DONE]\n\n`;
}

/**
 * Cuts the stream if no byte arrives for IDLE_TIMEOUT_MS — a crashed backend
 * (stuck model, LiteLLM no longer responding) would otherwise leave the
 * request open indefinitely, exhausting the Next.js server's connections.
 * Never affects an ACTIVE stream, even a very long one (large max_tokens): only
 * inactivity triggers the abort, not the total duration.
 */
function withIdleTimeout(body: ReadableStream<Uint8Array>): ReadableStream<Uint8Array> {
  const reader = body.getReader();
  return new ReadableStream({
    async pull(controller) {
      const timer = setTimeout(() => {
        controller.error(new Error("upstream idle timeout"));
        reader.cancel().catch(() => {});
      }, IDLE_TIMEOUT_MS);
      try {
        const { value, done } = await reader.read();
        clearTimeout(timer);
        if (done) {
          controller.close();
        } else {
          controller.enqueue(value);
        }
      } catch (e) {
        clearTimeout(timer);
        controller.error(e);
      }
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
}

/**
 * Relays a POST request to a Flask SSE endpoint, streaming the
 * response without buffering it (see the comments in app/playground/chat
 * and app/support/chat for why this isn't done via next.config.ts
 * or proxy.ts). Bounded by a connection timeout (the backend must respond
 * within 15s) then by an idle timeout once the stream has started.
 */
// Borne dure du corps relayé : la limite `proxyClientMaxBodySize` (20 Mo dans
// next.config.ts) est appliquée APRÈS la lecture, on l'applique donc AVANT.
const MAX_BODY_BYTES = 20 * 1024 * 1024;
const WHOAMI_TIMEOUT_MS = 3000;

export async function proxySSE(request: Request, path: string): Promise<Response> {
  // 1) Ne RIEN tamponner avant de savoir que le corps est borné et la session
  // valide (audit du 2026-10-02) : c'est la seule route publiée du frontend qui
  // lit un corps arbitraire, et `request.arrayBuffer()` le chargeait en mémoire
  // AVANT tout contrôle — n'importe quel appelant anonyme faisait donc travailler
  // le proxy pour 20 Mo. Le contrôle de session évite en plus au portail de
  // recevoir le corps (une redirection 401 est ce que `authFetch` attend).
  const annonce = Number(request.headers.get("content-length") || 0);
  if (Number.isFinite(annonce) && annonce > MAX_BODY_BYTES) {
    return new Response(sseErrorFrame("Requête trop volumineuse (20 Mo maximum)."), {
      status: 413,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }
  const verif = await fetch(`${BACKEND_URL}/api/whoami`, {
    headers: { Cookie: request.headers.get("cookie") || "" },
    signal: AbortSignal.timeout(WHOAMI_TIMEOUT_MS),
  }).catch(() => null);
  // 401/403 SEULEMENT : tout autre code (503 de maintenance, 500…) doit continuer
  // vers le backend, qui répondra ce qu'il doit répondre. Ce garde-fou ne remplace
  // pas l'autorisation — il évite le travail inutile.
  if (verif && (verif.status === 401 || verif.status === 403)) {
    return new Response(sseErrorFrame("Session expirée — reconnecte-toi."), {
      status: 401,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }
  // arrayBuffer(), not text(): /api/ocr/extract posts a binary
  // multipart/form-data (the image) — .text() would decode those bytes as UTF-8
  // and corrupt the upload. An ArrayBuffer passes through intact whatever the
  // content (text JSON as for playground/support, or binary here).
  const body = await request.arrayBuffer();
  // Un `content-length` absent (transfert par blocs) ou menteur reste possible :
  // on revérifie sur la taille RÉELLE, avant tout appel à l'amont.
  if (body.byteLength > MAX_BODY_BYTES) {
    return new Response(sseErrorFrame("Requête trop volumineuse (20 Mo maximum)."), {
      status: 413,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }
  const connectController = new AbortController();
  const connectTimer = setTimeout(() => connectController.abort(), CONNECT_TIMEOUT_MS);

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": request.headers.get("content-type") || "application/json",
        "X-CSRFToken": request.headers.get("x-csrftoken") || "",
        Cookie: request.headers.get("cookie") || "",
      },
      body,
      signal: connectController.signal,
    });
  } catch {
    return new Response(sseErrorFrame("Le serveur ne répond pas — réessaie dans un instant."), {
      status: 502,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  } finally {
    // Disarms the connection timeout once the headers are received: the rest
    // of the stream is bounded by withIdleTimeout (inactivity), not by a total
    // duration, so a long but active generation is never cut off.
    clearTimeout(connectTimer);
  }

  return new Response(upstream.body ? withIdleTimeout(upstream.body) : null, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}

/**
 * Relays a GET request (EventSource) to a Flask SSE endpoint — same
 * reasoning as proxySSE, adapted for a GET with no body or CSRF (EventSource
 * can't set any custom header, so nothing to forward besides the session
 * cookie). Flask itself relays a keep-alive ("): ping") every 50ms
 * from vllm-runner, so even this generous idle timeout never
 * fires on a healthy connection — only on a truly dead one.
 */
export async function proxySSEGet(request: Request, path: string): Promise<Response> {
  const connectController = new AbortController();
  const connectTimer = setTimeout(() => connectController.abort(), CONNECT_TIMEOUT_MS);

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_URL}${path}`, {
      method: "GET",
      headers: { Cookie: request.headers.get("cookie") || "" },
      signal: connectController.signal,
    });
  } catch {
    return new Response(sseErrorFrame("Le serveur ne répond pas — réessaie dans un instant."), {
      status: 502,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  } finally {
    clearTimeout(connectTimer);
  }

  return new Response(upstream.body ? withIdleTimeout(upstream.body) : null, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
