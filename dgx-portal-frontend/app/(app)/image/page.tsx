"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { Grid } from "@astryxdesign/core/Grid";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { useToast } from "@astryxdesign/core/Toast";
import { PhotoIcon, MoonIcon, ArrowDownTrayIcon, ArrowPathIcon, StopIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { DictateButton } from "../_components/DictateButton";
import { ModelRequestButton } from "../_components/ModelRequestButton";

type JobStatus = "idle" | "pending" | "running" | "done" | "error" | "cancelled";
type HistoryItem = { prompt_id: string; prompt: string; status: string; created_at: string; count?: number; done_count?: number };
type RunningModel = { name: string; kind: string; exposed: boolean };

const BATCH_CHOICES = [1, 2, 3, 4];

const STATUS_LABEL: Record<string, string> = {
  pending: "En file d'attente…",
  running: "Génération en cours…",
  done: "Image prête.",
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

export default function ImagePage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [prompt, setPrompt] = useState("");
  const [status, setStatus] = useState<JobStatus>("idle");
  const [promptId, setPromptId] = useState<string | null>(null);
  const [batch, setBatch] = useState(1);          // count chosen for the next generation
  const [jobCount, setJobCount] = useState(1);    // count of the currently-viewed job
  const [doneCount, setDoneCount] = useState(0);  // images produced so far for it
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
    fetch("/api/image/history", { credentials: "include" })
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
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "image")))
      .catch(() => setAvailable(null));
  }, []);

  // Un génération peut partir du formulaire (état courant) ou d'un « Réessayer »
  // sur un item d'historique échoué (prompt/batch fournis en dur — setState étant
  // asynchrone, on passe la valeur à la requête plutôt que de relire l'état).
  async function generate(opts?: { prompt?: string; batch?: number }) {
    const p = (opts?.prompt ?? prompt).trim();
    const b = Math.max(1, opts?.batch ?? batch);
    if (!p) return;
    if (opts?.prompt !== undefined) setPrompt(opts.prompt);
    if (opts?.batch !== undefined) setBatch(b);
    setStatus("pending");
    setPromptId(null);
    setJobCount(b);
    setDoneCount(0);
    try {
      const res = await postFormData<{ prompt_id?: string; count?: number; error?: string }>(
        "/api/image/generate",
        csrf,
        { prompt: p, count: String(b) },
      );
      if (!res.prompt_id) {
        showToast({ body: res.error ? t(res.error) : t("Échec de la génération."), type: "error" });
        setStatus("error");
        return;
      }
      setPromptId(res.prompt_id);
      setJobCount(res.count ?? b);
      loadHistory();
      pollRef.current = setInterval(async () => {
        const r = await fetch(`/api/image/status/${res.prompt_id}`, { credentials: "include" });
        const st = await r.json();
        if (typeof st.count === "number") setJobCount(st.count);
        if (typeof st.done_count === "number") setDoneCount(st.done_count);
        if (st.status === "done" || st.status === "error") {
          stopPolling();
          setStatus(st.status);
          loadHistory();
          if (st.status === "error") showToast({ body: t("La génération a échoué."), type: "error" });
        } else {
          setStatus(st.status);
        }
      }, 3000);
    } catch {
      setStatus("error");
      showToast({ body: t("Service de génération injoignable."), type: "error" });
    }
  }

  // Same-origin file → a plain anchor with `download` forces a real save
  // (Content-Disposition-free) instead of the browser opening it inline.
  function downloadImage(idx: number) {
    if (!promptId) return;
    const a = document.createElement("a");
    a.href = `/image/file/${promptId}/${idx}`;
    a.download = `image-${promptId}-${idx + 1}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function viewHistoryItem(item: HistoryItem) {
    stopPolling();
    setPromptId(item.prompt_id);
    setStatus(item.status as JobStatus);
    setJobCount(item.count ?? 1);
    setDoneCount(item.done_count ?? (item.status === "done" ? (item.count ?? 1) : 0));
  }

  // Relance une génération échouée avec le même prompt & le même nombre d'images.
  function retryItem(item: HistoryItem) {
    generate({ prompt: item.prompt, batch: item.count ?? 1 });
  }

  // Arrête la génération en cours (coopératif : la suite du lot est interrompue).
  async function cancel() {
    if (!promptId) return;
    setCancelling(true);
    try {
      await fetch(`/api/image/cancel/${promptId}`, {
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
          {/* Chargement : squelettes (available démarre à null) pour éviter le
              flash du formulaire avant l'EmptyState. */}
          {available === null ? (
            <VStack hAlign="center" width="100%">
              <VStack gap={5} maxWidth={720} width="100%">
                <VStack gap={2}>
                  <Skeleton height={28} width={260} />
                  <Skeleton height={14} width={340} />
                </VStack>
                <Card>
                  <VStack gap={4}>
                    <Skeleton height={16} width={160} />
                    <Skeleton height={120} radius={2} />
                  </VStack>
                </Card>
              </VStack>
            </VStack>
          ) : available === false && history.length === 0 ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle image n'est disponible")}
              description={t("Demande à un admin d'ajouter un modèle image pour utiliser cette page.")}
              actions={<ModelRequestButton category="image" showText={false} />}
            />
          ) : (
            <VStack hAlign="center" width="100%">
              <VStack gap={5} maxWidth={720} width="100%">
                <VStack gap={1}>
                  <Heading level={1}>{t("Génération d'image")}</Heading>
                  <Text type="supporting" color="secondary">
                    {t("Une description → une image générée localement sur le GPU.")}
                  </Text>
                </VStack>

                {available === false ? (
                  <Card>
                    <VStack gap={4}>
                      <HStack gap={3} vAlign="center">
                        <Icon icon={MoonIcon} size="md" color="secondary" />
                        <VStack gap={0}>
                          <Text weight="semibold">{t("Aucun modèle image chargé")}</Text>
                          <Text type="supporting" color="secondary">
                            {t("La génération est indisponible pour l'instant, mais tu peux revoir tes images précédentes ci-dessous.")}
                          </Text>
                        </VStack>
                      </HStack>
                      <ModelRequestButton category="image" />
                    </VStack>
                  </Card>
                ) : (
                  <Card>
                    <VStack gap={4}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <Text type="supporting" color="secondary">{t("Décris l'image")}</Text>
                        <DictateButton dictation={dictation} isDisabled={isBusy} />
                      </HStack>
                      <TextArea
                        label={t("Décris l'image")}
                        isLabelHidden
                        value={prompt}
                        onChange={setPrompt}
                        placeholder={t("Ex : un renard roux dans la neige, style photo réaliste, lumière douce.")}
                        maxLength={10000}
                        isDisabled={isBusy}
                        isRequired
                      />
                      <HStack hAlign="between" vAlign="center" gap={2} wrap="wrap">
                        <Text type="supporting" color="secondary">{t("Nombre d'images")}</Text>
                        <SegmentedControl
                          label={t("Nombre d'images")}
                          value={String(batch)}
                          onChange={(v) => setBatch(Number(v) || 1)}
                        >
                          {BATCH_CHOICES.map((n) => (
                            <SegmentedControlItem key={n} value={String(n)} label={String(n)} isDisabled={isBusy} />
                          ))}
                        </SegmentedControl>
                      </HStack>
                      <Button
                        label={batch > 1 ? t("Générer {n} images").replace("{n}", String(batch)) : t("Générer")}
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
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <StatusDot
                          variant={status === "done" ? "success" : status === "error" ? "error" : status === "cancelled" ? "neutral" : "accent"}
                          label={
                            isBusy && jobCount > 1
                              ? t("En cours")
                              : t(STATUS_LABEL[status] ?? status)
                          }
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
                      {isBusy && jobCount > 1 && (
                        <ProgressBar
                          label={t("Progression de la génération")}
                          value={doneCount}
                          max={jobCount}
                          variant="accent"
                          hasValueLabel
                          formatValueLabel={(v, m) => `${v}/${m}`}
                        />
                      )}
                      {promptId && (jobCount > 1 || isBusy) ? (
                        <Grid columns={{ minWidth: 220, max: 2 }} gap={3}>
                          {Array.from({ length: jobCount }).map((_, idx) => (
                            <VStack key={idx} gap={2}>
                              <AspectRatio ratio={1} fit="contain">
                                {idx < doneCount ? (
                                  // eslint-disable-next-line @next/next/no-img-element
                                  <img src={`/image/file/${promptId}/${idx}`} alt={`${prompt} (${idx + 1})`} />
                                ) : (
                                  <VStack className="video-generating" height="100%" width="100%" hAlign="center" vAlign="center" gap={2}>
                                    <Icon icon={PhotoIcon} size="lg" color="secondary" />
                                  </VStack>
                                )}
                              </AspectRatio>
                              {idx < doneCount && (
                                <Button
                                  label={t("Télécharger")}
                                  variant="secondary"
                                  size="sm"
                                  icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                                  onClick={() => downloadImage(idx)}
                                />
                              )}
                            </VStack>
                          ))}
                        </Grid>
                      ) : status === "done" && promptId ? (
                        <VStack gap={2}>
                          <AspectRatio ratio={1} fit="contain">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={`/image/file/${promptId}/0`} alt={prompt} />
                          </AspectRatio>
                          <Button
                            label={t("Télécharger")}
                            variant="secondary"
                            size="sm"
                            icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                            onClick={() => downloadImage(0)}
                          />
                        </VStack>
                      ) : (
                        <AspectRatio ratio={1} fit="contain">
                          <VStack className="video-generating" height="100%" width="100%" hAlign="center" vAlign="center" gap={2}>
                            <Icon icon={PhotoIcon} size="lg" color="secondary" />
                          </VStack>
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
                      {t("Historique")} ({history.length})
                    </Text>
                    <VStack gap={0}>
                      {history.map((h) => (
                        <Item
                          key={h.prompt_id}
                          label={h.prompt}
                          labelLines={1}
                          description={
                            new Date(h.created_at).toLocaleString("fr-FR") +
                            ((h.count ?? 1) > 1 ? ` · ${h.count} ${t("images")}` : "")
                          }
                          startContent={<PhotoIcon width={20} height={20} />}
                          endContent={
                            <HStack gap={2} vAlign="center">
                              <StatusDot
                                variant={h.status === "done" ? "success" : h.status === "error" ? "error" : h.status === "cancelled" ? "neutral" : "accent"}
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
