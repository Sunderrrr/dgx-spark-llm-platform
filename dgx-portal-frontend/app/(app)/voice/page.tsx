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
} from "@heroicons/react/24/outline";
import { Selector } from "@astryxdesign/core/Selector";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT, useLang } from "@/lib/i18n";
import { startRecording, type Recorder } from "@/lib/audioRecorder";

type HistoryItem = { id: number; text: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video" | "voice"; exposed: boolean };

/** Chatterbox rejette tout échantillon de 5 s ou moins (assertion côté modèle). */
const MIN_SECONDS = 6;
const MAX_SECONDS = 60;

function formatSeconds(s: number) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

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

  // Le micro doit être relâché et l'objet URL révoqué même si l'utilisateur
  // quitte la page en plein enregistrement.
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

  // Compteur d'enregistrement + coupure automatique à MAX_SECONDS.
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

  useEffect(loadHistory, []);

  useEffect(() => {
    fetch("/api/home", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "voice")))
      .catch(() => setAvailable(null));
  }, []);

  // Les langues dépendent de la variante chargée (turbo/original = anglais
  // seul, multilingual = 23) : on part de la langue de l'interface si le
  // modèle la connaît, sinon de l'anglais.
  useEffect(() => {
    fetch("/api/voice/languages", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : {}))
      .then((d: Record<string, string>) => {
        setLanguages(d);
        setLanguage((cur) => (d[cur] ? cur : d[uiLang] ? uiLang : "en"));
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
      });
      if (!res.id) {
        // Les messages d'erreur viennent de Flask, en français : t() les traduit
        // quand une entrée existe, et retombe sur l'original sinon (par ex. un
        // motif de refus relayé tel quel par le service voix).
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
          {available === false ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle vocal n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle vocal pour utiliser cette page.")}
            />
          ) : (
            <VStack gap={5} maxWidth={720}>
              <VStack gap={1}>
                <Heading level={1}>{t("Clonage de voix — Chatterbox")}</Heading>
                <Text type="supporting" color="secondary">
                  {t("Un échantillon vocal de plus de 5 secondes, un texte, → le texte lu avec cette voix. Génère localement sur le GPU, quelques secondes suffisent.")}
                </Text>
              </VStack>

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
                      description={t("WAV ou MP3 — 15 Mo max, plus de 5 secondes de voix claire.")}
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
                                label={t("Trop court : le modèle exige plus de 5 secondes de voix.")}
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
                  {Object.keys(languages).length > 1 && (
                    <Selector
                      label={t("Langue du texte")}
                      value={language}
                      onChange={(v) => setLanguage(v ?? "en")}
                      options={Object.entries(languages)
                        .map(([code, name]) => ({ value: code, label: name }))
                        .sort((a, b) => a.label.localeCompare(b.label))}
                      isDisabled={isLoading}
                    />
                  )}
                  <TextArea
                    label={t("Texte à lire")}
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

              {isLoading && (
                <Card>
                  <VStack gap={2}>
                    <Text>{t("Génération en cours…")}</Text>
                    <ProgressBar label={t("Progression")} isIndeterminate variant="accent" />
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
                      />
                    ))}
                  </VStack>
                </VStack>
              )}
            </VStack>
          )}
        </LayoutContent>
      }
    />
  );
}
