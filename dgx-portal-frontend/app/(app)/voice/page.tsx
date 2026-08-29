"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { FileInput } from "@astryxdesign/core/FileInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Badge } from "@astryxdesign/core/Badge";
import { useToast } from "@astryxdesign/core/Toast";
import {
  SpeakerWaveIcon,
  MoonIcon,
  MicrophoneIcon,
  StopIcon,
  ArrowUpTrayIcon,
  ArrowPathIcon,
  ClipboardIcon,
} from "@heroicons/react/24/outline";
import { Selector } from "@astryxdesign/core/Selector";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT, useLang } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { DictateButton } from "../_components/DictateButton";
import { ModelRequestButton } from "../_components/ModelRequestButton";
import { startRecording, type Recorder } from "@/lib/audioRecorder";

type HistoryItem = { id: number; text: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video" | "voice"; exposed: boolean };

/** Chatterbox rejects any sample of 5 s or less (model-side assertion). */
const MIN_SECONDS = 6;
const MAX_SECONDS = 60;

function formatSeconds(s: number) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/* Deterministic noise (classic sine-based hash): server and client MUST
 * draw exactly the same values, otherwise React hydration diverges — hence
 * this rather than Math.random(), which would also break the render purity
 * rule. */
function noise(i: number, seed: number) {
  const x = Math.sin(i * 12.9898 + seed) * 43758.5453;
  return x - Math.floor(x);
}

/* Each bar has its own amplitude AND its own duration. The duration does all
 * the work: with different durations the bars drift continuously relative to
 * one another, so the pattern never repeats. With a single duration and a
 * simple phase offset, you get a nice scrolling sine wave — regular and
 * mechanical. */
const WAVE_BARS = Array.from({ length: 40 }, (_, i) => ({
  amp: 0.3 + noise(i, 1) * 0.7,
  dur: 0.85 + noise(i, 2) * 1.1,
  delay: -noise(i, 3) * 2,
}));

export default function VoicePage() {
  const t = useT();
  const { lang: uiLang } = useLang();
  const csrf = useCsrf();
  const showToast = useToast();
  const [mode, setMode] = useState<"upload" | "record">("record");
  const [reference, setReference] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [languages, setLanguages] = useState<Record<string, string>>({});
  const [language, setLanguage] = useState<string>(uiLang);
  const [supportsRefText, setSupportsRefText] = useState(false);
  const [refText, setRefText] = useState("");
  const [engine, setEngine] = useState<string>("");
  const dictation = useDictation({ value: text, onChange: setText, csrf });
  const [isRecording, setIsRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [recordedUrl, setRecordedUrl] = useState<string | null>(null);
  const recorderRef = useRef<Recorder | null>(null);
  const recordedUrlRef = useRef<string | null>(null);

  const setRecorded = useCallback((file: File | null) => {
    if (recordedUrlRef.current) URL.revokeObjectURL(recordedUrlRef.current);
    recordedUrlRef.current = file ? URL.createObjectURL(file) : null;
    setRecordedUrl(recordedUrlRef.current);
    setReference(file);
  }, []);

  // The mic must be released and the object URL revoked even if the user
  // leaves the page mid-recording.
  useEffect(() => () => {
    recorderRef.current?.cancel();
    if (recordedUrlRef.current) URL.revokeObjectURL(recordedUrlRef.current);
  }, []);

  const stopRecording = useCallback(async () => {
    const rec = recorderRef.current;
    if (!rec) return;
    recorderRef.current = null;
    setIsRecording(false);
    try {
      const file = await rec.stop();
      setRecorded(file);
    } catch {
      showToast({ body: t("Impossible de convertir l'enregistrement."), type: "error" });
    }
  }, [setRecorded, showToast, t]);

  // Recording counter + automatic cutoff at MAX_SECONDS.
  useEffect(() => {
    if (!isRecording) return;
    const id = setInterval(() => {
      setElapsed((e) => {
        const next = e + 1;
        if (next >= MAX_SECONDS) void stopRecording();
        return next;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [isRecording, stopRecording]);

  async function beginRecording() {
    setRecorded(null);
    setElapsed(0);
    try {
      recorderRef.current = await startRecording();
      setIsRecording(true);
    } catch {
      showToast({
        body: t("Micro inaccessible — autorise l'accès au microphone dans ton navigateur."),
        type: "error",
      });
    }
  }

  function switchMode(next: "upload" | "record") {
    recorderRef.current?.cancel();
    recorderRef.current = null;
    setIsRecording(false);
    setElapsed(0);
    setRecorded(null);
    setMode(next);
  }

  function loadHistory() {
    fetch("/api/voice/history", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setHistory)
      .catch(() => {});
  }

  function copyText(s: string) {
    navigator.clipboard?.writeText(s).then(() => showToast({ body: t("Copié."), type: "info" }));
  }

  useEffect(loadHistory, []);

  useEffect(() => {
    fetch("/api/home", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "voice")))
      .catch(() => setAvailable(null));
  }, []);

  // The languages depend on the loaded variant (turbo/original = English
  // only, multilingual = 23): we start from the UI language if the model
  // knows it, otherwise English.
  useEffect(() => {
    fetch("/api/voice/info", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: { engine: string; languages: Record<string, string>; supports_ref_text: boolean } | null) => {
        if (!d) return;
        setEngine(d.engine || "");
        setLanguages(d.languages || {});
        setSupportsRefText(!!d.supports_ref_text);
        setLanguage((cur) =>
          d.languages?.[cur] ? cur : d.languages?.[uiLang] ? uiLang : d.languages?.auto ? "auto" : "en",
        );
      })
      .catch(() => {});
  }, [uiLang]);

  async function generate() {
    if (!reference || !text.trim() || !csrf) return;
    setIsLoading(true);
    setCurrentId(null);
    try {
      const res = await postFormData<{ id?: number; error?: string }>("/api/voice/generate", csrf, {
        reference,
        text,
        language,
        ref_text: refText,
      });
      if (!res.id) {
        // The error messages come from Flask, in French: t() translates them
        // when an entry exists, and falls back to the original otherwise (e.g. a
        // refusal reason relayed as-is by the voice service).
        showToast({ body: res.error ? t(res.error) : t("La génération a échoué."), type: "error" });
        return;
      }
      setCurrentId(res.id);
      loadHistory();
    } catch {
      showToast({ body: t("Service voix injoignable."), type: "error" });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          {available === false && history.length === 0 ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle vocal n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle vocal pour utiliser cette page.")}
            />
          ) : (
            // Centered column, like on the video page.
            <VStack hAlign="center" width="100%">
            <VStack gap={5} maxWidth={720} width="100%">
              <VStack gap={1}>
                <Heading level={1}>
                  {t("Clonage de voix")}
                  {engine ? ` — ${engine === "qwen3-tts" ? "Qwen3-TTS" : "Chatterbox"}` : ""}
                </Heading>
                <Text type="supporting" color="secondary">
                  {t("Un échantillon de ta voix, un texte, → le texte lu avec cette voix. Génère localement sur le GPU, quelques secondes suffisent.")}
                </Text>
              </VStack>

              {available === false ? (
                // No voice model loaded: the generation form is unavailable, but
                // past creations stay viewable/playable/copyable below (served
                // from disk, no model needed).
                <Card>
                  <VStack gap={4}>
                    <HStack gap={3} vAlign="center">
                      <Icon icon={MoonIcon} size="md" color="secondary" />
                      <VStack gap={0}>
                        <Text weight="semibold">{t("Aucun modèle vocal chargé")}</Text>
                        <Text type="supporting" color="secondary">
                          {t("La génération est indisponible pour l'instant, mais tu peux réécouter et copier tes créations précédentes ci-dessous.")}
                        </Text>
                      </VStack>
                    </HStack>
                    <ModelRequestButton category="voice" />
                  </VStack>
                </Card>
              ) : (
              <Card>
                <VStack gap={4}>
                  <SegmentedControl
                    label={t("Source de la voix")}
                    value={mode}
                    onChange={(v) => switchMode(v as "upload" | "record")}
                  >
                    <SegmentedControlItem
                      value="record"
                      label={t("Enregistrer au micro")}
                      icon={<Icon icon={MicrophoneIcon} size="sm" />}
                    />
                    <SegmentedControlItem
                      value="upload"
                      label={t("Importer un fichier")}
                      icon={<Icon icon={ArrowUpTrayIcon} size="sm" />}
                    />
                  </SegmentedControl>

                  {mode === "upload" ? (
                    <FileInput
                      label={t("Échantillon vocal de référence")}
                      value={reference}
                      onChange={(f) => setReference(f as File | null)}
                      accept="audio/wav,audio/x-wav,audio/mpeg,audio/mp3"
                      maxSize={15 * 1024 * 1024}
                      mode="dropzone"
                      description={t("WAV ou MP3 — 15 Mo max, au moins quelques secondes de voix claire.")}
                      isDisabled={isLoading}
                      isRequired
                    />
                  ) : (
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">
                        {t("Parle pendant 10 à 30 secondes pour un bon résultat — 1 minute maximum, l'enregistrement s'arrête tout seul.")}
                      </Text>
                      {isRecording ? (
                        <VStack gap={2}>
                          <HStack gap={2} vAlign="center">
                            <Text weight="semibold">{formatSeconds(elapsed)}</Text>
                            <Text type="supporting" color="secondary">
                              / {formatSeconds(MAX_SECONDS)}
                            </Text>
                          </HStack>
                          <ProgressBar
                            label={t("Enregistrement en cours")}
                            value={(elapsed / MAX_SECONDS) * 100}
                            variant={elapsed < MIN_SECONDS ? "warning" : "accent"}
                          />
                          <Button
                            label={t("Arrêter l'enregistrement")}
                            variant="primary"
                            icon={<Icon icon={StopIcon} size="sm" />}
                            onClick={stopRecording}
                          />
                        </VStack>
                      ) : recordedUrl ? (
                        <VStack gap={2}>
                          <Text type="supporting" color="secondary">
                            {t("Enregistrement")} · {formatSeconds(elapsed)}
                          </Text>
                          <audio src={recordedUrl} controls style={{ width: "100%" }} />
                          {elapsed < MIN_SECONDS && (
                            <HStack>
                              <Badge
                                label={t("Trop court : enregistre au moins 6 secondes de voix.")}
                                variant="warning"
                              />
                            </HStack>
                          )}
                          <Button
                            label={t("Réenregistrer")}
                            variant="secondary"
                            icon={<Icon icon={ArrowPathIcon} size="sm" />}
                            isDisabled={isLoading}
                            onClick={beginRecording}
                          />
                        </VStack>
                      ) : (
                        <Button
                          label={t("Démarrer l'enregistrement")}
                          variant="primary"
                          icon={<Icon icon={MicrophoneIcon} size="sm" />}
                          isDisabled={isLoading}
                          onClick={beginRecording}
                        />
                      )}
                    </VStack>
                  )}
                  {supportsRefText && (
                    <TextArea
                      label={t("Transcription de l'échantillon (optionnel)")}
                      value={refText}
                      onChange={setRefText}
                      placeholder={t("Recopie ici exactement ce que tu as dit dans l'enregistrement.")}
                      description={t("Améliore nettement la ressemblance. Sans elle, seule l'empreinte vocale est utilisée.")}
                      maxLength={2000}
                      rows={2}
                      isDisabled={isLoading}
                    />
                  )}
                  {Object.keys(languages).length > 1 && (
                    <Selector
                      label={t("Langue du texte")}
                      value={language}
                      onChange={(v) => setLanguage(v ?? "en")}
                      // Qwen returns the names in lowercase ("french"): we
                      // restore a capital letter for display.
                      options={Object.entries(languages)
                        .map(([code, name]) => ({
                          value: code,
                          label: name.charAt(0).toUpperCase() + name.slice(1),
                        }))
                        .sort((a, b) => a.label.localeCompare(b.label))}
                      isDisabled={isLoading}
                    />
                  )}
                  <HStack hAlign="between" vAlign="center" gap={2}>
                    <Text type="supporting" color="secondary">{t("Texte à lire")}</Text>
                    <DictateButton dictation={dictation} isDisabled={isLoading} />
                  </HStack>
                  <TextArea
                    label={t("Texte à lire")}
                    isLabelHidden
                    value={text}
                    onChange={setText}
                    placeholder={t("Ex : Bonjour, ceci est un test de clonage vocal.")}
                    maxLength={2000}
                    isDisabled={isLoading}
                    isRequired
                  />
                  <Button
                    label={t("Générer la voix")}
                    variant="primary"
                    onClick={generate}
                    isDisabled={
                      !reference ||
                      !text.trim() ||
                      isLoading ||
                      isRecording ||
                      (mode === "record" && elapsed < MIN_SECONDS)
                    }
                    isLoading={isLoading}
                  />
                </VStack>
              </Card>
              )}

              {isLoading && (
                <Card>
                  <VStack gap={2}>
                    <Text>{t("Génération en cours…")}</Text>
                    {/* Waveform occupying exactly the height of the audio
                        player that will replace it: nothing jumps when the
                        result arrives. Decorative in the ARIA sense (the text
                        above already carries the information), hence aria-hidden. */}
                    <HStack className="voice-wave" gap={1} vAlign="center" hAlign="center" aria-hidden>
                      {WAVE_BARS.map((b, i) => (
                        <span
                          key={i}
                          className="voice-wave-bar"
                          style={{
                            "--amp": b.amp,
                            "--dur": `${b.dur}s`,
                            "--delay": `${b.delay}s`,
                          } as React.CSSProperties}
                        />
                      ))}
                    </HStack>
                  </VStack>
                </Card>
              )}

              {currentId !== null && !isLoading && (
                <Card>
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Voix prête.")}</Text>
                    <audio src={`/voice/audio/${currentId}`} controls autoPlay style={{ width: "100%" }} />
                  </VStack>
                </Card>
              )}

              {history.length > 0 && (
                <VStack gap={2}>
                  <Text type="supporting" color="secondary">
                    {t("Historique")} ({history.length})
                  </Text>
                  <VStack gap={0}>
                    {history.map((h) => (
                      <Item
                        key={h.id}
                        label={h.text}
                        labelLines={1}
                        description={new Date(h.created_at).toLocaleString("fr-FR")}
                        startContent={<SpeakerWaveIcon width={20} height={20} />}
                        onClick={() => setCurrentId(h.id)}
                        isSelected={h.id === currentId}
                        endContent={
                          <Button
                            label={t("Copier")}
                            variant="ghost"
                            size="sm"
                            isIconOnly
                            icon={<Icon icon={ClipboardIcon} size="sm" />}
                            onClick={(e) => { e.stopPropagation(); copyText(h.text); }}
                          />
                        }
                      />
                    ))}
                  </VStack>
                </VStack>
              )}
            </VStack>
            </VStack>
          )}
        </LayoutContent>
      }
    />
  );
}
