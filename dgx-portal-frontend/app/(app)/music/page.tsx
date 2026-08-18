"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { Slider } from "@astryxdesign/core/Slider";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { MusicalNoteIcon, MoonIcon, ArrowDownTrayIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { DictateButton } from "../_components/DictateButton";

type JobStatus = "idle" | "running" | "done" | "error";
type HistoryItem = { job_id: string; prompt: string; lyrics: string | null; duration_s: number; status: string; created_at: string };
type RunningModel = { name: string; kind: string; exposed: boolean };

const STATUS_LABEL: Record<string, string> = {
  running: "Composition en cours…",
  done: "Morceau prêt.",
  error: "Échec de la génération.",
};
const STATUS_SHORT: Record<string, string> = { running: "En cours", done: "Terminé", error: "Erreur" };

export default function MusicPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [prompt, setPrompt] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [duration, setDuration] = useState(60);
  const [status, setStatus] = useState<JobStatus>("idle");
  const [jobId, setJobId] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const dictation = useDictation({ value: prompt, onChange: setPrompt, csrf });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  function loadHistory() {
    fetch("/api/music/history", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setHistory)
      .catch(() => {});
  }

  useEffect(() => { loadHistory(); return stopPolling; }, []);

  useEffect(() => {
    fetch("/api/home", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "music")))
      .catch(() => setAvailable(null));
  }, []);

  function downloadTrack(id: string) {
    const a = document.createElement("a");
    a.href = `/music/file/${id}`;
    a.download = `musique-${id}.wav`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function generate() {
    if (!prompt.trim() || !csrf) return;
    setStatus("running");
    setJobId(null);
    try {
      const res = await postFormData<{ job_id?: string; error?: string }>(
        "/api/music/generate", csrf,
        { prompt, lyrics, duration: String(duration) },
      );
      if (!res.job_id) {
        showToast({ body: res.error ? t(res.error) : t("Échec de la génération."), type: "error" });
        setStatus("error");
        return;
      }
      setJobId(res.job_id);
      loadHistory();
      // La composition peut durer plusieurs minutes : on interroge le statut
      // plutôt que de tenir une requête HTTP ouverte tout du long.
      pollRef.current = setInterval(async () => {
        const r = await fetch(`/api/music/status/${res.job_id}`, { credentials: "include" });
        const st = await r.json();
        if (st.status === "done" || st.status === "error") {
          stopPolling();
          setStatus(st.status);
          loadHistory();
          if (st.status === "error") showToast({ body: t("La génération a échoué."), type: "error" });
        }
      }, 4000);
    } catch {
      setStatus("error");
      showToast({ body: t("Service musique injoignable."), type: "error" });
    }
  }

  function viewHistoryItem(item: HistoryItem) {
    stopPolling();
    setJobId(item.job_id);
    setStatus(item.status as JobStatus);
  }

  const isBusy = status === "running";

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          {available === false && history.length === 0 ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle musique n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle musique pour utiliser cette page.")}
            />
          ) : (
            <VStack hAlign="center" width="100%">
              <VStack gap={5} maxWidth={720} width="100%">
                <VStack gap={1}>
                  <Heading level={1}>{t("Génération musicale")}</Heading>
                  <Text type="supporting" color="secondary">
                    {t("Une description de style et, si tu veux, des paroles → une chanson complète générée sur le GPU.")}
                  </Text>
                </VStack>

                {available === false ? (
                  <Card>
                    <HStack gap={3} vAlign="center">
                      <Icon icon={MoonIcon} size="md" color="secondary" />
                      <VStack gap={0}>
                        <Text weight="semibold">{t("Aucun modèle musique chargé")}</Text>
                        <Text type="supporting" color="secondary">
                          {t("La génération est indisponible pour l'instant, mais tu peux réécouter tes morceaux précédents ci-dessous.")}
                        </Text>
                      </VStack>
                    </HStack>
                  </Card>
                ) : (
                  <Card>
                    <VStack gap={4}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <Text type="supporting" color="secondary">{t("Décris la musique")}</Text>
                        <DictateButton dictation={dictation} isDisabled={isBusy} />
                      </HStack>
                      <TextArea
                        label={t("Décris la musique")}
                        isLabelHidden
                        value={prompt}
                        onChange={setPrompt}
                        placeholder={t("Ex : pop acoustique, 96 BPM, do majeur, voix féminine douce, guitare en arpèges, montée progressive vers le refrain.")}
                        maxLength={4000}
                        isDisabled={isBusy}
                        isRequired
                      />
                      <TextArea
                        label={t("Paroles (optionnel)")}
                        value={lyrics}
                        onChange={setLyrics}
                        placeholder={"[Couplet]\n…\n[Refrain]\n…"}
                        description={t("Utilise des balises de section : [Intro], [Couplet], [Refrain], [Pont], [Outro]. Sans paroles, le morceau sera instrumental.")}
                        maxLength={10000}
                        rows={4}
                        isDisabled={isBusy}
                      />
                      <Slider
                        label={`${t("Durée")} : ${duration} s`}
                        value={duration}
                        onChange={(v: number | [number, number]) => setDuration(Array.isArray(v) ? v[0] : v)}
                        min={15}
                        max={300}
                        step={15}
                        isDisabled={isBusy}
                      />
                      <Button
                        label={t("Composer")}
                        variant="primary"
                        onClick={generate}
                        isDisabled={!prompt.trim() || isBusy}
                        isLoading={isBusy}
                      />
                    </VStack>
                  </Card>
                )}

                {status !== "idle" && (
                  <Card>
                    <VStack gap={3}>
                      <StatusDot
                        variant={status === "done" ? "success" : status === "error" ? "error" : "accent"}
                        label={t(STATUS_LABEL[status] ?? status)}
                      />
                      {isBusy && (
                        <Text type="supporting" color="secondary">
                          {t("La composition d'un morceau prend plusieurs minutes — tu peux quitter la page, elle reste en cours.")}
                        </Text>
                      )}
                      {status === "done" && jobId && (
                        <VStack gap={2}>
                          <audio src={`/music/file/${jobId}`} controls style={{ width: "100%" }} />
                          <HStack>
                            <Button
                              label={t("Télécharger")}
                              variant="secondary"
                              size="sm"
                              icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                              onClick={() => downloadTrack(jobId)}
                            />
                          </HStack>
                        </VStack>
                      )}
                    </VStack>
                  </Card>
                )}

                {history.length > 0 && (
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">{t("Historique")} ({history.length})</Text>
                    <VStack gap={0}>
                      {history.map((h) => (
                        <Item
                          key={h.job_id}
                          label={h.prompt}
                          labelLines={1}
                          description={`${new Date(h.created_at).toLocaleString("fr-FR")} · ${h.duration_s}s`}
                          startContent={<MusicalNoteIcon width={20} height={20} />}
                          endContent={
                            <StatusDot
                              variant={h.status === "done" ? "success" : h.status === "error" ? "error" : "accent"}
                              label={t(STATUS_SHORT[h.status] ?? h.status)}
                            />
                          }
                          onClick={() => viewHistoryItem(h)}
                          isSelected={h.job_id === jobId}
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
