// Attachments of the playground: which files are accepted, which images the
// model can read, and the browser-side preparation of an image before it is
// sent. Extracted VERBATIM from PlaygroundPage.tsx (module-scope constants and
// pure helpers, no behaviour change) — the page keeps the handlers that decide
// and the state that collects them.

export const ATTACH_ACCEPT =
  ".md,.markdown,.txt,.text,.log,.logs,.err,.error,.out,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.py,.js,.ts,.jsx,.tsx,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.bash,.sql,.html,.css,.xml,.diff,.patch";

export const MAX_ATTACHMENT_BYTES = 96 * 1024;

/** Extensions that the `accept` attribute only SUGGESTS: a file picker
 *  always lets « Tous les fichiers » through, and drag-and-drop
 *  ignores it entirely. Without this check, an image was read as TEXT
 *  (`readAsText`) and its binary content went into the prompt as
 *  mojibake, with no warning at all. */
export const ATTACH_EXTENSIONS = ATTACH_ACCEPT.split(",").map((e) => e.trim().toLowerCase());

/** Images: accepted ONLY if the selected model reads them (`model_vision`).
 *  Downscaled in the browser before sending — a phone photo (4 000 px,
 *  5 Mo) would otherwise cost thousands of preload tokens and exceed
 *  the conversation save limit. 1 568 px is enough to read text
 *  or a screenshot. See `prepareImage`: an already reasonable image
 *  goes as is, without going through a canvas. */
export const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif";
export const IMAGE_EXTENSIONS = IMAGE_ACCEPT.split(",");
export const IMAGE_SIDE_MAX = 1568;
export const IMAGE_SOURCE_MAX_BYTES = 25 * 1024 * 1024;
export const IMAGES_PER_MESSAGE = 4;
/** Images replayed to the model per request: the most recent ones (same bound as the server). */
export const MAX_IMAGES_PER_REQUEST = 8;
/** Estimated weight of an image in the context window, in characters (~1 500 tokens). */
export const IMAGE_WEIGHT_CHARS = 1500 * 4;

/** Size beyond which an image is downscaled (the server rejects > ~2,2 Mo). */
export const ORIGINAL_IMAGE_MAX_BYTES = 2 * 1024 * 1024;

export function readDataUrl(file: Blob): Promise<string> {
  return new Promise((ok, ko) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => ko(r.error);
    r.readAsDataURL(file);
  });
}

/** Does the canvas really render what is drawn on it?
 *  Measured on 2026-10-01: on some browsers (canvas anti-tracking
 *  protections), reading back yields an EMPTY canvas — the image went out
 *  all black, and the model confidently described an invented scene. */
export function reliableCanvas(): boolean {
  try {
    const c = document.createElement("canvas");
    c.width = c.height = 4;
    const ctx = c.getContext("2d");
    if (!ctx) return false;
    ctx.fillStyle = "rgb(255,0,0)";
    ctx.fillRect(0, 0, 4, 4);
    const [r, g, b, a] = ctx.getImageData(1, 1, 1, 1).data;
    return r > 200 && g < 60 && b < 60 && a > 200;
  } catch {
    return false;
  }
}

/** The image to send, as a `data:` URL.
 *  An already reasonable image goes AS IS (no canvas: nothing to
 *  damage). Only a large image is downscaled to IMAGE_COTE_MAX in JPEG, and
 *  only if the canvas is reliable; otherwise we refuse rather than send
 *  a black image. */
export async function prepareImage(file: File): Promise<string> {
  const bmp = await createImageBitmap(file);
  const { width, height } = bmp;
  const typeOk = /^image\/(png|jpeg|webp|gif)$/.test(file.type);
  if (typeOk && file.size <= ORIGINAL_IMAGE_MAX_BYTES && Math.max(width, height) <= IMAGE_SIDE_MAX * 2) {
    bmp.close();
    return readDataUrl(file);
  }
  if (!reliableCanvas()) {
    bmp.close();
    throw new Error("canevas");
  }
  const scale = Math.min(1, IMAGE_SIDE_MAX / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas");
  ctx.fillStyle = "white";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  return canvas.toDataURL("image/jpeg", 0.85);
}
