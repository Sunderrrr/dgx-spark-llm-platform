"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { startRecording, type Recorder } from "./audioRecorder";
import { useLang } from "./i18n";

/**
 * Dictée vocale réutilisable (Playground, Voix, Vidéo).
 *
 * Le texte s'écrit AU FIL de la parole : toutes les 1,5 s on retranscrit tout
 * l'audio capté depuis le début et on réécrit la partie dictée du champ.
 * Whisper répond en ~0,2 s quelle que soit la longueur (jusqu'à 30 s), et lui
 * redonner tout le contexte lui permet de se corriger en cours de route.
 */
const POLL_MS = 1500;

type Options = {
  /** Valeur actuelle du champ ciblé. */
  value: string;
  /** Applique la nouvelle valeur (texte de départ + ce qui a été dicté). */
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
  // Texte présent avant le début de la dictée : chaque passe remplace la
  // précédente sans jamais effacer ce que l'utilisateur avait tapé.
  const baseRef = useRef("");
  // Une seule transcription en vol : la suivante n'part qu'au retour de la
  // précédente, ce qui régule la charge GPU sans réglage.
  const busyRef = useRef(false);
  // Incrémenté à chaque arrêt : une réponse arrivée trop tard appartient à une
  // dictée révolue et ne doit plus écrire dans le champ (typiquement quand on
  // envoie le message pendant que la transcription est encore en route).
  const runRef = useRef(0);
  // onChange/value changent à chaque frappe : on les lit par ref pour ne pas
  // relancer la boucle de sondage à chaque caractère.
  const onChangeRef = useRef(onChange);
  const valueRef = useRef(value);
  // Mis à jour APRÈS le rendu, jamais pendant : écrire dans une ref en plein
  // rendu casse les garanties de React (et la règle react-hooks/refs).
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

  // Relâche le micro si le composant disparaît en pleine dictée.
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
      if (run !== runRef.current) return true; // dictée abandonnée entre-temps
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
        // Un tour raté n'interrompt pas la dictée, le suivant reprendra.
      } finally {
        busyRef.current = false;
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [isRecording, transcribe]);

  /** Coupe le micro et abandonne : rien de plus ne sera écrit dans le champ. */
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
        // Passe finale sur l'enregistrement complet : elle fait autorité sur
        // les transcriptions intermédiaires, plus pauvres en contexte.
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
