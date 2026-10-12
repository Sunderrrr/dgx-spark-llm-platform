/* Voice mode — the text side, pure (no DOM, no audio): what is SAID out of a
 * model answer, and how it is cut into sentences sent one by one to the voice.
 * The same `speakable` text is what the screen shows, so the « already said »
 * highlight (grey → white) indexes the exact characters that were read. */

/** A model answer as it is read aloud: no markdown, no code, no URLs. The
 *  voice-mode instruction asks for plain speech already; this is the net for
 *  the answers that slip (and for the turns written before voice mode). */
export function speakable(md: string): string {
  return md
    .replace(/```[\s\S]*?(?:```|$)/g, " ")          // code blocks: never read
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")            // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")          // links → their text
    .replace(/https?:\/\/\S+/g, " ")                  // bare URLs
    .replace(/`([^`]*)`/g, "$1")                      // inline code → plain
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")               // headings
    .replace(/^\s{0,3}>\s?/gm, "")                    // quotes
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "")        // list markers
    .replace(/(\*\*|__|\*|_|~~)(?=\S)([^*_~\n]+?)\1/g, "$2") // emphasis
    .replace(/\|/g, " ")                              // table pipes
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

export type Sentence = { text: string; start: number; end: number };

/** Below this, a sentence waits to be joined to the next one: « Oui. » alone
 *  costs a whole request for half a second of audio. */
const MIN_CHARS = 14;

/** The complete sentences of `text` from `from` on. `final` (the answer has
 *  ended) also releases the unfinished tail. `end` is where the next call
 *  starts. Boundaries: . ! ? … (followed by a space or the end) and line
 *  breaks — never a decimal point (« 3.5 ») nor an abbreviation's dot inside a
 *  word. */
export function nextSentences(text: string, from: number, final: boolean): { sentences: Sentence[]; end: number } {
  const sentences: Sentence[] = [];
  const re = /[.!?…]+["»”')\]]*(?=\s|$)|\n/g;
  re.lastIndex = from;
  let start = from;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const stop = m.index + m[0].length;
    // A boundary at the very end of a still-growing answer may be the dot
    // of « 3. » before « 5 »: wait for what follows.
    if (!final && stop >= text.length) break;
    const piece = text.slice(start, stop);
    if (piece.trim().length < MIN_CHARS && !final) continue;   // join with the next
    if (piece.trim()) sentences.push({ text: piece.trim(), start, end: stop });
    start = stop;
  }
  if (final && text.slice(start).trim()) {
    sentences.push({ text: text.slice(start).trim(), start, end: text.length });
    start = text.length;
  }
  return { sentences, end: start };
}

/** Whisper's classic inventions on silence or noise: never sent as a turn. */
export function isPhantomTranscript(text: string): boolean {
  const t = text.trim().toLowerCase();
  if (!t) return true;
  return /amara\.org|sous-titr|subtitles? by|merci d'avoir regardé|thanks? for watching/.test(t)
    // « Thank you. » / « You » alone: what Whisper hears in a breath or the
    // room's noise (sent as a turn on 2026-10-13, mid French conversation).
    || /^(?:thank you|thanks|you|bye)[\s.!…]*$/.test(t)
    || /^[\s.,!?…-]*$/.test(t);
}
