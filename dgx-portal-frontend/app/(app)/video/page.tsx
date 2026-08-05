"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack } from "@astryxdesign/core/Stack";
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
import { useToast } from "@astryxdesign/core/Toast";
import { FilmIcon, MoonIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";
import { useT } from "@/lib/i18n";

type JobStatus = "idle" | "pending" | "running" | "done" | "error";
type HistoryItem = { prompt_id: string; prompt: string; status: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };

const DURATIONS = ["3", "5", "8", "10", "15"];

// Statuts renvoyés par ComfyUI. Ils s'affichaient tels quels ("running"),
// non traduits, dans la pastille de statut et dans l'historique.
const STATUS_LABEL: Record<string, string> = {
  pending: "En file d'attente…",
  running: "Génération en cours…",
  done: "Vidéo prête.",
  error: "Échec de la génération.",
};
const STATUS_SHORT: Record<string, string> = {
  pending: "En attente",
  running: "En cours",
  done: "Terminé",
  error: "Erreur",
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

  async function generate() {
    if (!prompt.trim()) return;
    setStatus("pending");
    setPromptId(null);
    try {
      const payload: Record<string, string | File> = { prompt, duration };
      if (image) payload.image = image;
      const res = await postFormData<{ prompt_id?: string; error?: string }>(
        "/api/video/generate",
        csrf,
        payload,
      );
      if (!res.prompt_id) {
        showToast({ body: res.error || t("Échec de la génération."), type: "error" });
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

  const isBusy = status === "pending" || status === "running";

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          {available === false ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle vidéo n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle vidéo pour utiliser cette page.")}
            />
          ) : (
          // Colonne centrée : à gauche d'un écran large, la page paraissait
          // décentrée alors que tout le reste de l'app est équilibré.
          <VStack hAlign="center" width="100%">
          <VStack gap={5} maxWidth={720} width="100%">
            <VStack gap={1}>
              <Heading level={1}>{t("Génération vidéo — MiniMax H3")}</Heading>
              <Text type="supporting" color="secondary">
                {t("Une description, avec ou sans image de référence, → une courte vidéo avec audio synchronisé. Génère localement sur le GPU, compte 5 à 10 minutes selon la charge.")}
              </Text>
            </VStack>

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
                <TextArea
                  label={t("Décris la scène")}
                  value={prompt}
                  onChange={setPrompt}
                  placeholder={t("Ex : un ballon rouge qui rebondit sur un sol blanc, caméra fixe.")}
                  maxLength={2000}
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
                  onClick={generate}
                  isDisabled={!prompt.trim() || isBusy}
                  isLoading={isBusy}
                />
              </VStack>
            </Card>

            {status !== "idle" && (
              <Card>
                <VStack gap={4}>
                  {/* Un seul porteur du statut : la pastille affichait le
                      libellé BRUT ("running") juste à côté du même statut
                      traduit, et le placeholder le répétait une troisième
                      fois. */}
                  <StatusDot
                    variant={status === "done" ? "success" : status === "error" ? "error" : "accent"}
                    label={t(STATUS_LABEL[status] ?? status)}
                  />
                  {/* Pendant la génération, on occupe déjà la place exacte de
                      la vidéo à venir (même 16/9) avec un shimmer : la barre
                      de progression indéterminée ne disait rien de plus et
                      faisait sauter la mise en page à l'arrivée du résultat. */}
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
                </VStack>
              </Card>
            )}

            {history.length > 0 && (
              <VStack gap={2}>
                <Text type="supporting" color="secondary">
                  {t("Historique (3 dernières)")}
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
                        <StatusDot
                          variant={
                            h.status === "done" ? "success" : h.status === "error" ? "error" : "accent"
                          }
                          label={t(STATUS_SHORT[h.status] ?? h.status)}
                        />
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
