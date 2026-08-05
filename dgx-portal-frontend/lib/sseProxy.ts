const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

const CONNECT_TIMEOUT_MS = 15_000; // délai pour recevoir les en-têtes de la réponse
const IDLE_TIMEOUT_MS = 60_000; // délai sans aucun octet reçu une fois le flux démarré

function sseErrorFrame(text: string): string {
  const payload = JSON.stringify({ choices: [{ delta: { content: text } }] });
  return `data: ${payload}\n\ndata: [DONE]\n\n`;
}

/**
 * Coupe le flux si aucun octet n'arrive pendant IDLE_TIMEOUT_MS — un backend
 * planté (modèle bloqué, LiteLLM qui ne répond plus) laisserait sinon la
 * requête ouverte indéfiniment, épuisant les connexions du serveur Next.js.
 * N'affecte jamais un flux ACTIF, même très long (gros max_tokens) : seule
 * l'inactivité déclenche l'abandon, pas la durée totale.
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
 * Relaie une requête POST vers un endpoint SSE de Flask, en streamant la
 * réponse sans la bufferiser (voir les commentaires dans app/playground/chat
 * et app/support/chat pour pourquoi ceci n'est pas fait via next.config.ts
 * ou proxy.ts). Borné par un timeout de connexion (le backend doit répondre
 * sous 15s) puis par un timeout d'inactivité une fois le flux démarré.
 */
export async function proxySSE(request: Request, path: string): Promise<Response> {
  // arrayBuffer(), pas text() : /api/ocr/extract poste un multipart/form-data
  // binaire (l'image) — .text() décoderait ces octets en UTF-8 et corromprait
  // l'upload. Un ArrayBuffer transite intact quel que soit le contenu (JSON
  // texte comme pour playground/support, ou binaire ici).
  const body = await request.arrayBuffer();
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
    // Désarme le timeout de connexion une fois les en-têtes reçus : la suite
    // du flux est bornée par withIdleTimeout (inactivité), pas par une durée
    // totale, pour ne jamais couper une génération longue mais active.
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
 * Relaie une requête GET (EventSource) vers un endpoint SSE de Flask — même
 * raisonnement que proxySSE, adapté pour un GET sans corps ni CSRF (EventSource
 * ne peut poser aucun header custom, donc rien à transmettre à part le cookie
 * de session). Flask relaie lui-même un keep-alive ("): ping") toutes les 50ms
 * depuis vllm-runner, donc même ce timeout d'inactivité généreux ne se
 * déclenche jamais sur une connexion saine — seulement sur une vraiment morte.
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
