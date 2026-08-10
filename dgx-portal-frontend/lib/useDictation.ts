"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { startRecording, type Recorder } from "./audioRecorder";
import { useLang } from "./i18n";

/**
 * Reusable voice dictation (Playground, Voice, Video).
 *
 * The text is written AS speech flows: every 1.5 s we re-transcribe all
 * the audio captured since the start and rewrite the dictated part of the field.
 * Whisper responds in ~0.2 s whatever the length (up to 30 s), and giving
 * it the whole context lets it correct itself along the way.
 */
const POLL_MS = 1500;

type Options = {
  /** Current value of the target field. */
  value: string;
  /** Applies the new value (starting text + what has been dictated). */
  onChange: (next: string) => void;
  csrf: string;
};

export function useDictation({ value, onChange, csrf }: Options) {
  const { lang } = useLang();
  const [available, setAvailable] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const recorderRef = useRef<Recorder | null>(null);
  // Text present before dictation started: each pass replaces the
  // previous one without ever erasing what the user had typed.
  const baseRef = useRef("");
  // A single transcription in flight: the next one only leaves on the return
  // of the previous, which regulates GPU load without any tuning.
  const busyRef = useRef(false);
  // Incremented on each stop: a response arriving too late belongs to a
  // bygone dictation and must no longer write into the field (typically when
  // the message is sent while the transcription is still under way).
  const runRef = useRef(0);
  // onChange/value change on every keystroke: we read them by ref so as not
  // to restart the polling loop on every character.
  const onChangeRef = useRef(onChange);
  const valueRef = useRef(value);
  // Updated AFTER render, never during: writing to a ref mid-render
  // breaks React's guarantees (and the react-hooks/refs rule).
  useEffect(() => {
    onChangeRef.current = onChange;
    valueRef.current = value;
  });

  useEffect(() => {
    fetch("/api/transcribe/available", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAvailable(!!d?.available))
      .catch(() => {});
  }, []);

  // Releases the mic if the component disappears mid-dictation.
  useEffect(() => () => recorderRef.current?.cancel(), []);

  const transcribe = useCallback(
    async (file: File, run: number): Promise<boolean> => {
      const body = new FormData();
      body.append("audio", file);
      body.append("language", lang);
      const res = await fetch("/api/transcribe", {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRFToken": csrf },
        body,
      });
      const data = await res.json();
      if (run !== runRef.current) return true; // dictation abandoned in the meantime
      if (!res.ok || typeof data.text !== "string") {
        setError(data?.error || null);
        return false;
      }
      const spoken = data.text.trim();
      const base = baseRef.current;
      onChangeRef.current(spoken ? (base ? `${base.trimEnd()} ${spoken}` : spoken) : base);
      return true;
    },
    [csrf, lang],
  );

  useEffect(() => {
    if (!isRecording) return;
    const run = runRef.current;
    const id = setInterval(async () => {
      if (busyRef.current || run !== runRef.current) return;
      const rec = recorderRef.current;
      if (!rec) return;
      busyRef.current = true;
      try {
        const partial = await rec.snapshot();
        if (partial) await transcribe(partial, run);
      } catch {
        // A failed round doesn't interrupt dictation, the next one will resume.
      } finally {
        busyRef.current = false;
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [isRecording, transcribe]);

  /** Cuts the mic and aborts: nothing more will be written into the field. */
  const cancel = useCallback(() => {
    runRef.current += 1;
    recorderRef.current?.cancel();
    recorderRef.current = null;
    setIsRecording(false);
    setIsTranscribing(false);
  }, []);

  const toggle = useCallback(async () => {
    setError(null);
    if (isRecording) {
      const rec = recorderRef.current;
      recorderRef.current = null;
      setIsRecording(false);
      if (!rec) return;
      const run = runRef.current;
      setIsTranscribing(true);
      try {
        // Final pass on the complete recording: it is authoritative over
        // the intermediate transcriptions, poorer in context.
        const file = await rec.stop();
        await transcribe(file, run);
      } catch {
        setError("");
      } finally {
        setIsTranscribing(false);
      }
      return;
    }
    try {
      recorderRef.current = await startRecording();
      baseRef.current = valueRef.current;
      setIsRecording(true);
    } catch {
      setError("mic");
    }
  }, [isRecording, transcribe]);

  return { available, isRecording, isTranscribing, error, toggle, cancel };
}

export type Dictation = ReturnType<typeof useDictation>;
