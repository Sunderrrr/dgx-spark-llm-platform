"use client";

// The playground's voice mode: a hands-free conversation with the model.
//
// You speak — the mic finds where your sentence ends, Whisper writes it (live,
// in your bubble), it goes out as an ordinary turn of THIS conversation. The
// answer is read sentence by sentence by Kyutai TTS while the model is still
// writing; on screen the whole answer is shown in grey and turns white as it
// is said. Speak over it and it stops (barge-in). A warm halo at the bottom
// breathes with whoever is talking.
//
// Everything stays in the conversation's history: closing the voice mode
// leaves the thread exactly as a written exchange would.

import { useCallback, useEffect, useRef, useState } from "react";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Selector } from "@astryxdesign/core/Selector";
import {
  MicrophoneIcon,
  SpeakerWaveIcon,
  SpeakerXMarkIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { fetchCsrfToken } from "@/lib/api";
import type { ChatMsg } from "@/lib/types";
import { MicListener, SpeechPlayer } from "@/lib/voiceAudio";
import { isPhantomTranscript, nextSentences, speakable, type Sentence } from "@/lib/voice-text";
import { ThinkingIndicator } from "../../_components/ThinkingIndicator";

type Phase = "preparing" | "listening" | "hearing" | "thinking" | "speaking";

type Props = {
  messages: ChatMsg[];
  streaming: boolean;
  model: string;
  csrf: string;
  /** Sends a spoken turn (voice-mode instruction, no thinking). */
  onSend: (text: string) => void;
  /** Stops the answer being generated (barge-in). */
  onInterrupt: () => void;
  onClose: () => void;
};

const VOICE_KEY = "pg_voice";
const VOICE_LABELS: Record<string, string> = { "fr-a": "A", "fr-b": "B", "fr-c": "C", "fr-d": "D" };
/** Messages kept on screen: the voice mode is the end of a conversation. */
const SHOWN_MESSAGES = 16;

async function transcribe(wav: Blob, csrf: string): Promise<{ text: string; starting?: boolean; error?: string }> {
  const body = new FormData();
  body.append("audio", new File([wav], "speech.wav", { type: "audio/wav" }));
  const res = await fetch("/api/transcribe", {
    method: "POST", credentials: "include", headers: { "X-CSRFToken": csrf }, body,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { text: "", error: data?.error || `HTTP ${res.status}` };
  return { text: typeof data.text === "string" ? data.text : "", starting: !!data.starting };
}

export function VoiceMode({ messages, streaming, model, csrf, onSend, onInterrupt, onClose }: Props) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>("preparing");
  const [notice, setNotice] = useState<string | null>(null);
  const [liveText, setLiveText] = useState("");
  const [micMuted, setMicMuted] = useState(false);
  const [speakerOn, setSpeakerOn] = useState(true);
  const [voices, setVoices] = useState<string[]>([]);
  const [voice, setVoice] = useState<string>(() => {
    try { return localStorage.getItem(VOICE_KEY) || "fr-a"; } catch { return "fr-a"; }
  });
  // Characters of the current answer already said (grey → white), and which
  // message that answer is (null: nothing being voiced).
  const [said, setSaid] = useState(0);
  const [voiced, setVoiced] = useState<number | null>(null);
  const [voiceAvailable, setVoiceAvailable] = useState(true);

  const micRef = useRef<MicListener | null>(null);
  const playerRef = useRef<SpeechPlayer | null>(null);
  const glowRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  // The answer being voiced: its index in `messages`, how far its text has
  // been cut into sentences, the sentences waiting for the voice.
  const turn = useRef<{ awaitFrom: number | null; index: number | null; cut: number; queue: Sentence[]; pumping: boolean }>(
    { awaitFrom: null, index: null, cut: 0, queue: [], pumping: false });
  const voiceOk = useRef(true);
  const failures = useRef(0);
  // A sound over the assistant, not yet confirmed as an interruption.
  const barge = useRef(false);
  // The « said » boundary in the answer: the view follows it as it moves.
  const markRef = useRef<HTMLElement | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const userScrolledAt = useRef(0);
  // Everything the mic callbacks read: they are set up ONCE, at opening —
  // a captured `onSend` was the first render's, with an empty CSRF token,
  // and every spoken turn failed (400 → « Erreur réseau »).
  const live = useRef({ messages, streaming, voice, speakerOn, phase, csrf, onSend, onInterrupt });
  useEffect(() => {
    live.current = { messages, streaming, voice, speakerOn, phase, csrf, onSend, onInterrupt };
  });

  // ── The voice: one sentence after another, each request as soon as the
  //    previous one has fully ARRIVED (the voice is faster than speech).
  const pump = useCallback(async () => {
    const tr = turn.current;
    const player = playerRef.current;
    if (tr.pumping || !player) return;
    tr.pumping = true;
    try {
      while (tr.queue.length) {
        const s = tr.queue.shift()!;
        if (!live.current.speakerOn || !voiceOk.current) continue;
        try {
          await player.speak(s.text, live.current.voice, live.current.csrf, s.start, s.end);
          failures.current = 0;
        } catch (e) {
          if ((e as Error).name === "AbortError") break;
          // One failed sentence is skipped, not the whole voice: a single
          // hiccup silenced the rest of the conversation (2026-10-13).
          failures.current += 1;
          if (failures.current >= 3) {
            voiceOk.current = false;
            setVoiceAvailable(false);
            setNotice(t("La voix ne répond pas : la suite s'affiche en texte."));
          }
        }
      }
    } finally {
      tr.pumping = false;
    }
  }, [t]);

  // ── New answer text → new complete sentences for the voice.
  useEffect(() => {
    const tr = turn.current;
    if (tr.awaitFrom !== null && messages.length > tr.awaitFrom + 1
        && messages[tr.awaitFrom + 1]?.role === "assistant") {
      tr.index = tr.awaitFrom + 1;
      setVoiced(tr.index);
      tr.awaitFrom = null;
      tr.cut = 0;
      tr.queue = [];
      playerRef.current?.reset();
      setSaid(0);
    }
    if (tr.index === null) return;
    const m = messages[tr.index];
    // An error is shown, never read; an empty spoken turn is asked again by
    // the page, and its new answer replaces this one from the start.
    if (!m || m.role !== "assistant" || m.isError) return;
    const text = speakable(m.content);
    if (text.length < tr.cut) {
      tr.cut = 0;
      tr.queue = [];
      playerRef.current?.reset();
      setSaid(0);
    }
    const final = !streaming || tr.index !== messages.length - 1;
    const { sentences, end } = nextSentences(text, tr.cut, final);
    tr.cut = end;
    if (sentences.length) {
      tr.queue.push(...sentences);
      void pump();
    }
  }, [messages, streaming, pump]);

  // ── Barge-in: the user talks over the answer.
  const interrupt = useCallback(() => {
    const tr = turn.current;
    playerRef.current?.stop();
    tr.queue = [];
    if (live.current.streaming) live.current.onInterrupt();
    if (tr.index !== null) setSaid(Number.MAX_SAFE_INTEGER);   // what was written stays readable
    tr.index = null;
    setVoiced(null);
  }, []);

  // ── Open: voice service, transcription warm-up, microphone.
  useEffect(() => {
    let closed = false;
    const player = new SpeechPlayer();
    playerRef.current = player;
    void player.resume();
    (async () => {
      // The page's token may still be loading when the voice mode opens.
      const token = live.current.csrf || await fetchCsrfToken().catch(() => "");
      const res = await fetch("/api/voice/chat/start", {
        method: "POST", credentials: "include", headers: { "X-CSRFToken": token },
      }).catch(() => null);
      const data = res ? await res.json().catch(() => ({})) : {};
      if (closed) return;
      if (res?.ok && data.ready) {
        setVoices(data.voices ?? []);
      } else {
        voiceOk.current = false;
        setVoiceAvailable(false);
        setSpeakerOn(false);
        setNotice(data?.code === "voice_engine"
          ? t("La voix n'est pas disponible : le moteur vocal chargé n'est pas Kyutai TTS. Un admin peut le lancer depuis Admin → Modèles. Les réponses s'affichent en texte.")
          : t("La voix n'est pas disponible pour l'instant ({reason}). Les réponses s'affichent en texte.")
              .replace("{reason}", data?.error || t("service arrêté")));
      }
    })();
    (async () => {
      let mic: MicListener;
      try {
        mic = await MicListener.open();
      } catch {
        if (!closed) setNotice(t("Le micro est inaccessible : autorise-le dans le navigateur."));
        return;
      }
      if (closed) { mic.close(); return; }
      micRef.current = mic;
      // Over the assistant, a sound only LOWERS its voice; the answer is cut
      // once the speech is confirmed (CONFIRM_MS). A blip — a cough, the
      // room, its own echo — restores it: « …les ressources et la » was cut
      // by a sound that transcribed to nothing (2026-10-13).
      mic.onSpeechStart = () => {
        const p = live.current.phase;
        if (p === "thinking" || p === "speaking") {
          barge.current = true;
          playerRef.current?.duck(true);
          return;
        }
        setLiveText("");
        setPhase("hearing");
      };
      mic.onSpeechConfirmed = () => {
        if (!barge.current) return;
        barge.current = false;
        interrupt();
        setLiveText("");
        setPhase("hearing");
      };
      mic.onSpeechEnd = async (wav, confirmed) => {
        if (barge.current && !confirmed) {
          barge.current = false;
          playerRef.current?.duck(false);
          return;
        }
        if (!wav) {
          if (live.current.phase === "hearing") setPhase("listening");
          return;
        }
        setPhase("thinking");
        let r = await transcribe(wav, live.current.csrf);
        // First use: the transcription service is still loading — wait for it.
        for (let k = 0; r.starting && k < 30; k++) {
          setNotice(t("Démarrage de la transcription…"));
          await new Promise((ok) => setTimeout(ok, 2000));
          r = await transcribe(wav, live.current.csrf);
        }
        setNotice((n) => (n === t("Démarrage de la transcription…") ? null : n));
        setLiveText("");
        if (r.error) {
          setNotice(r.error);
          setPhase("listening");
          return;
        }
        if (isPhantomTranscript(r.text) || live.current.streaming) {
          setPhase("listening");
          return;
        }
        turn.current.awaitFrom = live.current.messages.length;
        live.current.onSend(r.text.trim());
      };
      setPhase("listening");
    })();
    return () => {
      closed = true;
      micRef.current?.close();
      micRef.current = null;
      player.close();
      playerRef.current = null;
    };
    // Opened once: everything that changes is read through `live`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Live transcription of the sentence being spoken (your bubble).
  useEffect(() => {
    if (phase !== "hearing") return;
    let busy = false;
    const id = setInterval(async () => {
      const snap = micRef.current?.snapshot();
      if (busy || !snap) return;
      busy = true;
      try {
        const r = await transcribe(snap, live.current.csrf);
        if (live.current.phase === "hearing" && r.text && !isPhantomTranscript(r.text)) setLiveText(r.text.trim());
      } finally {
        busy = false;
      }
    }, 1000);
    return () => clearInterval(id);
  }, [phase]);

  // ── Animation loop: halo level, « said » highlight, end of the turn.
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const mic = micRef.current;
      const player = playerRef.current;
      const tr = turn.current;
      const playing = !!player && !player.idle();
      if (mic) mic.guard = playing;
      const level = Math.max(live.current.phase === "hearing" ? mic?.level ?? 0 : 0, playing ? player!.level() : 0);
      glowRef.current?.style.setProperty("--voice-level", level.toFixed(3));
      if (tr.index !== null && player) {
        const n = player.spokenChars();
        setSaid((prev) => (prev === n ? prev : n));
        if (playing && live.current.phase === "thinking") setPhase("speaking");
        const done = !live.current.streaming && !tr.queue.length && !tr.pumping && !playing;
        if (done && (live.current.phase === "speaking" || live.current.phase === "thinking")) {
          tr.index = null;
          setVoiced(null);
          setSaid(Number.MAX_SAFE_INTEGER);
          setPhase("listening");
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  // Escape closes, like every overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The view follows the voice: the « said » boundary is kept in the upper
  // part of the screen as it moves down (it stayed off-screen, the reader had
  // to scroll by hand). A manual scroll pauses the following for a few
  // seconds; otherwise a new message brings the end into view.
  useEffect(() => {
    if (Date.now() - userScrolledAt.current < 4000) return;
    const box = threadRef.current;
    const mark = markRef.current;
    if (voiced !== null && box && mark) {
      const b = box.getBoundingClientRect();
      const y = mark.getBoundingClientRect().top - b.top;
      if (y > b.height * 0.55 || y < 0) {
        box.scrollTo({ top: box.scrollTop + y - b.height * 0.3, behavior: "smooth" });
      }
      return;
    }
    endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages.length, liveText, phase, said, voiced]);

  const toggleMic = () => {
    const next = !micMuted;
    setMicMuted(next);
    if (micRef.current) micRef.current.muted = next;
    if (next && phase === "hearing") setPhase("listening");
  };
  const toggleSpeaker = () => {
    const next = !speakerOn;
    setSpeakerOn(next);
    playerRef.current?.setMuted(!next);
  };
  const pickVoice = (v: string | null) => {
    if (!v) return;
    setVoice(v);
    try { localStorage.setItem(VOICE_KEY, v); } catch { /* private mode */ }
  };

  const shown = messages
    .map((m, i) => ({ m, i }))
    .filter(({ m }) => !m.hidden && (m.role === "user" || m.role === "assistant") && m.content.trim())
    .slice(-SHOWN_MESSAGES);
  const voicedIndex = voiced;
  const status =
    micMuted ? t("Micro coupé")
    : phase === "preparing" ? t("Préparation…")
    : phase === "listening" ? t("Je t'écoute…")
    : phase === "hearing" ? t("Je t'écoute…")
    : phase === "speaking" ? t("Parle pour l'interrompre")
    : "";

  return (
    <VStack className="voice-mode" role="dialog" aria-modal="true" aria-label={t("Mode vocal")}>
      <VStack className="voice-glow" ref={glowRef} aria-hidden="true">{null}</VStack>
      <HStack className="voice-top" hAlign="end" gap={2} vAlign="center">
        {voices.length > 1 && (
          <Selector
            label={t("Voix")}
            isLabelHidden
            size="sm"
            value={voice}
            onChange={pickVoice}
            options={voices.map((v) => ({ value: v, label: `${t("Voix")} ${VOICE_LABELS[v] ?? v}` }))}
          />
        )}
      </HStack>
      <VStack className="voice-thread" gap={6} ref={threadRef}
        onWheel={() => { userScrolledAt.current = Date.now(); }}
        onTouchMove={() => { userScrolledAt.current = Date.now(); }}>
        {shown.map(({ m, i }) =>
          m.role === "user" ? (
            <HStack key={i} hAlign="end">
              <VStack className="voice-user"><Text>{m.content}</Text></VStack>
            </HStack>
          ) : (
            <Text key={i} className="voice-answer">
              {i === voicedIndex && speakerOn ? (
                <>
                  <Text className="voice-said">{speakable(m.content).slice(0, said)}</Text>
                  <Text ref={markRef} className="voice-mark">{""}</Text>
                  <Text className="voice-unsaid">{speakable(m.content).slice(said)}</Text>
                </>
              ) : speakable(m.content)}
            </Text>
          ),
        )}
        {phase === "hearing" && (
          <HStack hAlign="end">
            <VStack className="voice-user voice-user-live"><Text>{liveText || "…"}</Text></VStack>
          </HStack>
        )}
        {phase === "thinking" && !(voicedIndex !== null && messages[voicedIndex]?.content) && (
          <HStack hAlign="start"><ThinkingIndicator /></HStack>
        )}
        {notice && <Text className="voice-notice" type="supporting">{notice}</Text>}
        <VStack ref={endRef}>{null}</VStack>
      </VStack>
      <VStack className="voice-bar" gap={2}>
        <Text type="supporting" className="voice-status">{status}</Text>
        <HStack vAlign="center" gap={3}>
          <StackItem size="fill">
            <Text type="supporting" className="voice-model">{model}</Text>
          </StackItem>
          <Button
            label={micMuted ? t("Réactiver le micro") : t("Couper le micro")}
            variant={micMuted ? "secondary" : "primary"}
            isIconOnly
            className={`voice-mic${phase === "hearing" ? " voice-mic-hot" : ""}`}
            icon={<Icon icon={MicrophoneIcon} size="md" />}
            onClick={toggleMic}
          />
          <StackItem size="fill">
            <HStack hAlign="end" gap={2}>
              <Button
                label={speakerOn ? t("Couper la voix") : t("Réactiver la voix")}
                variant="secondary"
                isIconOnly
                isDisabled={!voiceAvailable && !speakerOn}
                icon={<Icon icon={speakerOn ? SpeakerWaveIcon : SpeakerXMarkIcon} size="md" />}
                onClick={toggleSpeaker}
              />
              <Button
                label={t("Quitter le mode vocal")}
                variant="primary"
                isIconOnly
                icon={<Icon icon={XMarkIcon} size="md" />}
                onClick={onClose}
              />
            </HStack>
          </StackItem>
        </HStack>
      </VStack>
    </VStack>
  );
}
