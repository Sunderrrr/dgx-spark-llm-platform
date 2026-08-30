"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { FileInput } from "@astryxdesign/core/FileInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { useToast } from "@astryxdesign/core/Toast";
import { FilmIcon, MoonIcon, ArrowPathIcon, StopIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { DictateButton } from "../_components/DictateButton";
import { ModelRequestButton } from "../_components/ModelRequestButton";

type JobStatus = "idle" | "pending" | "running" | "done" | "error" | "cancelled";
type HistoryItem = { prompt_id: string; prompt: string; status: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };

const DURATIONS = ["3", "5", "8", "10", "15"];

// Statuses returned by ComfyUI. They used to show as-is ("running"),
// untranslated, in the status dot and in the history.
const STATUS_LABEL: Record<string, string> = {
  pending: "En file d'attente…",
  running: "Génération en cours…",
  done: "Vidéo prête.",
  error: "Échec de la génération.",
  cancelled: "Génération annulée.",
};
const STATUS_SHORT: Record<string, string> = {
  pending: "En attente",
  running: "En cours",
  done: "Terminé",
  error: "Erreur",
  cancelled: "Annulé",
};

export default function VideoPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [image, setImage] = useState<File | null>(null);
  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState("5");
  const [status, setStatus] = useState<JobStatus>("idle");
  const [promptId, setPromptId] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const dictation = useDictation({ value: prompt, onChange: setPrompt, csrf });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function loadHistory() {
    fetch("/api/video/history", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setHistory)
      .catch(() => {});
  }

  useEffect(() => {
    loadHistory();
    return stopPolling;
  }, []);

  useEffect(() => {
    fetch("/api/home", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "video")))
      .catch(() => setAvailable(null));
  }, []);

  // `prompt` est passable en surcharge (« Réessayer » d'un item d'historique) —
  // setState étant asynchrone, on lit la valeur du paramètre pour la requête.
  async function generate(opts?: { prompt?: string }) {
    const p = (opts?.prompt ?? prompt).trim();
    if (!p) return;
    if (opts?.prompt !== undefined) setPrompt(opts.prompt);
    setStatus("pending");
    setPromptId(null);
    try {
      const payload: Record<string, string | File> = { prompt: p, duration };
      if (image) payload.image = image;
      const res = await postFormData<{ prompt_id?: string; error?: string }>(
        "/api/video/generate",
        csrf,
        payload,
      );
      if (!res.prompt_id) {
        showToast({ body: res.error ? t(res.error) : t("Échec de la génération."), type: "error" });
        setStatus("error");
        return;
      }
      setPromptId(res.prompt_id);
      loadHistory();
      pollRef.current = setInterval(async () => {
        const r = await fetch(`/api/video/status/${res.prompt_id}`, { credentials: "include" });
        const st = await r.json();
        if (st.status === "done" || st.status === "error") {
          stopPolling();
          setStatus(st.status);
          loadHistory();
          if (st.status === "error") showToast({ body: t("La génération a échoué."), type: "error" });
        } else {
          setStatus(st.status);
        }
      }, 5000);
    } catch {
      setStatus("error");
      showToast({ body: t("ComfyUI injoignable."), type: "error" });
    }
  }

  function viewHistoryItem(item: HistoryItem) {
    stopPolling();
    setPromptId(item.prompt_id);
    setStatus(item.status as JobStatus);
  }

  // Relance une génération échouée : on reprend le prompt, la durée et l'image
  // de référence courants (la référence d'origine n'est pas restituable).
  function retryItem(item: HistoryItem) {
    generate({ prompt: item.prompt });
  }

  // Arrête la génération vidéo en cours (interrupt ComfyUI) ou en attente.
  async function cancel() {
    if (!promptId) return;
    setCancelling(true);
    try {
      await fetch(`/api/video/cancel/${promptId}`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRFToken": csrf },
      });
      setStatus("cancelled");
      stopPolling();
      loadHistory();
      showToast({ body: t("Génération annulée."), type: "info" });
    } catch {
      showToast({ body: t("Impossible d'annuler."), type: "error" });
    } finally {
      setCancelling(false);
    }
  }

  const isBusy = status === "pending" || status === "running";

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          {available === null ? (
            <VStack hAlign="center" width="100%">
              <VStack gap={5} maxWidth={720} width="100%">
                <VStack gap={2}>
                  <Skeleton height={28} width={300} />
                  <Skeleton height={14} width={360} />
                </VStack>
                <Card>
                  <VStack gap={4}>
                    <Skeleton height={16} width={180} />
                    <Skeleton height={120} radius={2} />
                  </VStack>
                </Card>
              </VStack>
            </VStack>
          ) : available === false && history.length === 0 ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle vidéo n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle vidéo pour utiliser cette page.")}
              actions={<ModelRequestButton category="video" showText={false} />}
            />
          ) : (
          // Centered column: on the left of a wide screen the page looked
          // off-center while the rest of the app is balanced.
          <VStack hAlign="center" width="100%">
          <VStack gap={5} maxWidth={720} width="100%">
            <VStack gap={1}>
              <Heading level={1}>{t("Génération vidéo — MiniMax H3")}</Heading>
              <Text type="supporting" color="secondary">
                {t("Une description, avec ou sans image de référence, → une courte vidéo avec audio synchronisé. Génère localement sur le GPU, compte 5 à 10 minutes selon la charge.")}
              </Text>
            </VStack>

            {available === false ? (
              <Card>
                <VStack gap={4}>
                  <HStack gap={3} vAlign="center">
                    <Icon icon={MoonIcon} size="md" color="secondary" />
                    <VStack gap={0}>
                      <Text weight="semibold">{t("Aucun modèle vidéo chargé")}</Text>
                      <Text type="supporting" color="secondary">
                        {t("La génération est indisponible pour l'instant, mais tu peux revoir tes vidéos précédentes ci-dessous.")}
                      </Text>
                    </VStack>
                  </HStack>
                  <ModelRequestButton category="video" />
                </VStack>
              </Card>
            ) : (
            <Card>
              <VStack gap={4}>
                <FileInput
                  label={t("Image de référence (optionnel)")}
                  value={image}
                  onChange={(f) => setImage(f as File | null)}
                  accept="image/png,image/jpeg,image/webp"
                  maxSize={15 * 1024 * 1024}
                  mode="dropzone"
                  description={t("PNG, JPEG ou WebP — 15 Mo max. Sans image, génère depuis le texte seul.")}
                  isDisabled={isBusy}
                />
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <Text type="supporting" color="secondary">{t("Décris la scène")}</Text>
                  <DictateButton dictation={dictation} isDisabled={isBusy} />
                </HStack>
                <TextArea
                  label={t("Décris la scène")}
                  isLabelHidden
                  value={prompt}
                  onChange={setPrompt}
                  placeholder={t("Ex : un ballon rouge qui rebondit sur un sol blanc, caméra fixe.")}
                  maxLength={10000}
                  isDisabled={isBusy}
                  isRequired
                />
                <Selector
                  label={t("Durée")}
                  value={duration}
                  onChange={setDuration}
                  options={DURATIONS.map((d) => ({ label: `${d}s`, value: d }))}
                  isDisabled={isBusy}
                />
                <Button
                  label={t("Générer")}
                  variant="primary"
                  onClick={() => generate()}
                  isDisabled={!prompt.trim() || isBusy}
                  isLoading={isBusy}
                />
              </VStack>
            </Card>
            )}

            {status !== "idle" && (
              <Card>
                <VStack gap={4}>
                  {/* A single status carrier: the dot showed the RAW label
                      ("running") right next to the same translated status,
                      and the placeholder repeated it a third time. */}
                  <HStack hAlign="between" vAlign="center" gap={2}>
                    <StatusDot
                      variant={status === "done" ? "success" : status === "error" ? "error" : status === "cancelled" ? "neutral" : "accent"}
                      label={t(STATUS_LABEL[status] ?? status)}
                    />
                    {isBusy && (
                      <Button
                        label={t("Arrêter")}
                        variant="secondary"
                        size="sm"
                        icon={<Icon icon={StopIcon} size="sm" />}
                        onClick={cancel}
                        isDisabled={cancelling}
                        isLoading={cancelling}
                      />
                    )}
                  </HStack>
                  {/* During generation we already occupy the exact spot of
                      the upcoming video (same 16/9) with a shimmer: the
                      indeterminate progress bar said nothing more and made
                      the layout jump when the result arrived. */}
                  {isBusy && (
                    <AspectRatio ratio={16 / 9} fit="contain">
                      <VStack
                        className="video-generating"
                        height="100%"
                        width="100%"
                        hAlign="center"
                        vAlign="center"
                        gap={2}
                      >
                        <Icon icon={FilmIcon} size="lg" color="secondary" />
                      </VStack>
                    </AspectRatio>
                  )}
                  {status === "done" && promptId && (
                    <AspectRatio ratio={16 / 9} fit="contain">
                      <video src={`/video/file/${promptId}`} controls autoPlay loop />
                    </AspectRatio>
                  )}
                  {status === "error" && (
                    <Button
                      label={t("Réessayer")}
                      variant="secondary"
                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                      onClick={() => generate()}
                    />
                  )}
                </VStack>
              </Card>
            )}

            {history.length > 0 && (
              <VStack gap={2}>
                <Text type="supporting" color="secondary">
                  {/* Real counter rather than a hardcoded number, which
                      becomes wrong at the slightest change of limit. */}
                  {t("Historique")} ({history.length})
                </Text>
                <VStack gap={0}>
                  {history.map((h) => (
                    <Item
                      key={h.prompt_id}
                      label={h.prompt}
                      labelLines={1}
                      description={new Date(h.created_at).toLocaleString("fr-FR")}
                      startContent={<FilmIcon width={20} height={20} />}
                      endContent={
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={
                              h.status === "done" ? "success" : h.status === "error" ? "error" : h.status === "cancelled" ? "neutral" : "accent"
                            }
                            label={t(STATUS_SHORT[h.status] ?? h.status)}
                          />
                          {h.status === "error" && (
                            <Button
                              label={t("Réessayer")}
                              variant="ghost"
                              size="sm"
                              isIconOnly
                              icon={<Icon icon={ArrowPathIcon} size="sm" />}
                              onClick={() => retryItem(h)}
                            />
                          )}
                        </HStack>
                      }
                      onClick={() => viewHistoryItem(h)}
                      isSelected={h.prompt_id === promptId}
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
