/* Voice mode — the text side, pure (no DOM, no audio): what is SAID out of a
 * model answer, and how it is cut into sentences sent one by one to the voice.
 * The same `speakable` text is what the screen shows, so the « already said »
 * highlight (grey → white) indexes the exact characters that were read. */

/** The system instruction of a spoken turn.
 *
 *  Framed as a text-to-speech constraint, NOT as « this conversation is
 *  spoken »: told that it is in a spoken conversation, qwen38-27b treats
 *  backchannels (« Mm-hmm », « Ouais, j'aime bien ») as needing no reply and
 *  ends its turn at once — <|im_end|> at 97 % (measured 2026-10-13; this
 *  wording: 0 empty out of 32, 2026-10-14).
 *
 *  Long content is done NOW, in parts. The previous wording (« two or three
 *  short sentences, under 60 words ») made a long request impossible to
 *  honour, so the model announced it instead — « je vais rédiger le sommaire
 *  des 20 chapitres » — five times in a row, never writing it (a real
 *  session, 2026-10-14). Replayed on the same requests (book, guide, story,
 *  « fais-moi un PDF », « vas-y »), this wording writes the content every
 *  time (75-200 words, then « on continue ? »), « oui, continue » resumes at
 *  the next part, and small talk stays at 25-40 words. */
export const VOICE_INSTRUCTION = `Your reply will be read by a text-to-speech engine, and the user's messages come from speech recognition. In conversation, answer in one to three short sentences. When the user asks for something long — a story, a guide, an explanation, a plan, chapters — do it now, in this reply. Never announce what you are about to do, never say you will write it in a later message (there is none unless the user speaks), and never ask permission to start when the request is clear. Speak long content in parts of about 150 words: give the first part, then ask in a few words whether to go on; when the user agrees, continue exactly where you stopped. Write as people speak: plain sentences, with no markdown, lists, headings, tables, emoji, code or URLs; say a list as sentences ("first…, then…"). You cannot create files such as a PDF here: say so in a few words and speak the content instead. Speech recognition makes mistakes: a message that looks garbled or comes out in another language is a recognition error — answer what the user most likely meant, and always in the language the conversation is held in.`;

/** Added to the last user message of a RETRY only (an empty spoken turn): in
 *  the message itself it got an answer 8 times out of 8 where the system
 *  instruction alone still left 5 out of 8 empty. */
export const VOICE_NUDGE = "\n\n[Reply to this message out loud: at least one short sentence.]";

/** A model answer as it is read aloud: no markdown, no code, no URLs. The
 *  voice-mode instruction asks for plain speech already; this is the net for
 *  the answers that slip (and for the turns written before voice mode). */
export function speakable(md: string): string {
  return md
    .replace(/^\s*<think>[\s\S]*?(?:<\/think>|$)/i, " ") // a thought written into the answer
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
    || /^[\s.,!?…-]*$/.test(t)
    || looping(t);
}

/** Whisper's other invention on a long stretch of noise or echo: one phrase
 *  repeated over and over (« Thank you. Thank you. … » ×100, « Bye-bye. »
 *  ×90 — 2026-10-13). A person does not say the same words ten times. */
function looping(t: string): boolean {
  const words = t.split(/[^\p{L}\p{N}']+/u).filter(Boolean);
  if (words.length < 8) return false;
  if (new Set(words).size / words.length < 0.3) return true;
  return /(.{3,40}?)(?:[\s.,!?…]+\1){3,}/.test(t);
}
