// Playground messages: what is kept, what comes back, and following an answer
// that is still being written (its checkpoint is `pending` on the server).

import { useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { fetchConversation } from "@/lib/conversations";
import type { ChatMsg, Conversation } from "@/lib/types";

/** Image budget of a SAVED conversation (characters): the server bounds
 *  the conversation to 2 M and the form field to 4 Mo. Beyond that, the
 *  oldest images are no longer kept (the text, however, is). */
export const IMAGES_SAVE_BUDGET = 1_500_000;

/** The answer is checkpointed on the server while it streams, every this many ms. */
export const CHECKPOINT_MS = 3000;
/** How often a page that finds a checkpoint re-reads the conversation. */
const PENDING_POLL_MS = 2000;
/** Re-reads before a checkpoint is given up (a stream that died leaves one behind). */
const PENDING_MAX_POLLS = 300;

/** The turn being streamed, kept OUTSIDE the page: leaving the playground does
 *  not stop the stream, so a page that comes back must be able to show its
 *  counters and to stop it. Module state survives navigation inside the app
 *  (a reload ends the stream anyway). */
export type LiveTurn = {
  convId: string | null;
  controller: AbortController;
  /** When the first token arrived (ms, performance.now()); null until then. */
  start: number | null;
  chars: number;
  charsPerToken: number;
  /** The answer and its thinking as streamed so far, word by word. */
  text: string;
  reasoning: string;
};

let liveTurn: LiveTurn | null = null;

export function beginLiveTurn(convId: string | null, controller: AbortController, charsPerToken: number): LiveTurn {
  liveTurn = { convId, controller, start: null, chars: 0, charsPerToken, text: "", reasoning: "" };
  return liveTurn;
}

export function getLiveTurn(): LiveTurn | null {
  return liveTurn;
}

export function endLiveTurn(turn: LiveTurn) {
  if (liveTurn === turn) liveTurn = null;
}

/** True when the answer is stuck repeating the same line: one line filling at
 *  least 8 of the last 20 non-empty lines. A degenerate loop, not a list. */
export function isLooping(text: string): boolean {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean).slice(-20);
  if (lines.length < 20) return false;
  const counts = new Map<string, number>();
  for (const l of lines) counts.set(l, (counts.get(l) ?? 0) + 1);
  return Math.max(...counts.values()) >= 8;
}

/** Keeps images only on the most recent messages, within `max`
 *  images or `budget` characters. Text is never touched. */
export function recentImages<M extends { images?: string[] }>(msgs: M[], max: number, budget = Infinity): M[] {
  let remaining = max;
  let weight = 0;
  const out = [...msgs];
  for (let i = out.length - 1; i >= 0; i--) {
    const imgs = out[i].images;
    if (!imgs?.length) continue;
    const guard: string[] = [];
    for (let k = imgs.length - 1; k >= 0 && remaining > 0; k--) {
      if (weight + imgs[k].length > budget) { remaining = 0; break; }
      guard.unshift(imgs[k]);
      weight += imgs[k].length;
      remaining--;
    }
    out[i] = { ...out[i], images: guard.length ? guard : undefined };
  }
  return out;
}

/** A saved message back in the thread. Everything the server keeps comes
 *  back: only `role`/`content`/`hidden` used to, so a reopened conversation
 *  lost its EDITED files (the next edit worked on the old version), and a
 *  cut answer looked finished again. */
export function fromSaved(m: Conversation["messages"][number]): ChatMsg {
  return {
    role: m.role, content: m.content, hidden: m.hidden,
    ...(m.reasoning ? { reasoning: m.reasoning } : {}),
    ...(m.isError ? { isError: true } : {}),
    ...(m.truncated ? { truncated: true } : {}),
    ...(m.pending ? { pending: true } : {}),
    ...(m.images?.length ? { images: m.images } : {}),
    ...(m.fileEdits?.length ? { fileEdits: m.fileEdits } : {}),
  };
}

/** Title and summary only read the text: no need to ship megabytes of images through them. */
export function withoutImages(msgs: ChatMsg[]) {
  return msgs.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden }));
}

/** Messages as they are saved (flags + images within the budget). */
export function forSave(msgs: ChatMsg[]) {
  return recentImages(
    msgs.map((m) => ({
      role: m.role, content: m.content, hidden: m.hidden, reasoning: m.reasoning,
      truncated: m.truncated, isError: m.isError, pending: m.pending, images: m.images,
    })),
    Infinity, IMAGES_SAVE_BUDGET);
}

/** An answer still streaming: its checkpoint is `pending` on the server. While
 *  the stream lives (`getLiveTurn`), this page adopts it — it shows the
 *  streaming state, the counters and the Stop button of that stream — and it
 *  re-reads the conversation so the text grows as it is written. When the
 *  stream ends, the final save replaces the checkpoint and the page follows. */
export function usePendingFollow(
  messages: ChatMsg[],
  streaming: boolean,
  currentId: string | null,
  currentIdRef: MutableRefObject<string | null>,
  setMessages: Dispatch<SetStateAction<ChatMsg[]>>,
  setStreaming: Dispatch<SetStateAction<boolean>>,
  abortRef: MutableRefObject<AbortController | null>,
) {
  const pollsLeft = useRef(PENDING_MAX_POLLS);
  // Adoption: a live stream of this conversation, not started by this page. Its
  // text comes from the stream itself, word by word; the server is not re-read
  // while the stream lives (its checkpoint lags by several seconds).
  useEffect(() => {
    const turn = getLiveTurn();
    if (streaming || !turn || !currentId || turn.convId !== currentId) return;
    abortRef.current = turn.controller;
    setStreaming(true);
    const id = window.setInterval(() => {
      const live = getLiveTurn();
      if (live !== turn) {
        window.clearInterval(id);
        abortRef.current = null;
        setStreaming(false);
        // The stream is over: the saved answer replaces what was streamed here.
        // If the save is not there yet, the checkpoint poll picks it up.
        const done = currentId;
        if (done) {
          void fetchConversation(done).then((conv) => {
            if (conv && currentIdRef.current === done) setMessages(conv.messages.map(fromSaved));
          });
        }
        return;
      }
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant") return prev;
        if (last.content === live.text && last.reasoning === (live.reasoning || undefined)) return prev;
        return [...prev.slice(0, -1), { ...last, content: live.text, reasoning: live.reasoning || undefined, pending: undefined }];
      });
    }, 250);
    return () => window.clearInterval(id);
  }, [streaming, currentId, currentIdRef, abortRef, setStreaming, setMessages]);

  useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last?.pending || !currentId) {
      pollsLeft.current = PENDING_MAX_POLLS;
      return;
    }
    // The stream of this very conversation is followed live above, not by polling.
    if (getLiveTurn()?.convId === currentId) return;
    // A checkpoint left behind by a dead stream is not followed forever.
    if (pollsLeft.current <= 0) return;
    pollsLeft.current -= 1;
    const id = currentId;
    const timer = window.setTimeout(() => {
      void fetchConversation(id).then((conv) => {
        if (conv && currentIdRef.current === id) setMessages(conv.messages.map(fromSaved));
      });
    }, PENDING_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [messages, currentId, currentIdRef, setMessages]);
}
