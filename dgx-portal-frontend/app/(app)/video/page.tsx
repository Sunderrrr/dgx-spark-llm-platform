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
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { FilmIcon, MoonIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormData } from "@/lib/api";

type JobStatus = "idle" | "pending" | "running" | "done" | "error";
type HistoryItem = { prompt_id: string; prompt: string; status: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };

const DURATIONS = ["3", "5", "8", "10", "15"];

export default function VideoPage() {
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
    if (!image || !prompt.trim()) return;
    setStatus("pending");
    setPromptId(null);
    try {
      const res = await postFormData<{ prompt_id?: string; error?: string }>(
        "/api/video/generate",
        csrf,
        { image, prompt, duration },
      );
      if (!res.prompt_id) {
        showToast({ body: res.error || "Échec de la génération.", type: "error" });
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
          if (st.status === "error") showToast({ body: "La génération a échoué.", type: "error" });
        } else {
          setStatus(st.status);
        }
      }, 5000);
    } catch {
      setStatus("error");
      showToast({ body: "ComfyUI injoignable.", type: "error" });
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
              title="Aucun modèle vidéo n'est disponible"
              description="Demande à un admin de démarrer un modèle vidéo pour utiliser cette page."
            />
          ) : (
          <VStack gap={5} maxWidth={720}>
            <VStack gap={1}>
              <Heading level={1}>Génération vidéo — MiniMax H3</Heading>
              <Text type="supporting" color="secondary">
                Une image de référence + une description → une courte vidéo avec audio synchronisé.
                Génère localement sur le GPU, compte 5 à 10 minutes selon la charge.
              </Text>
            </VStack>

            <Card>
              <VStack gap={4}>
                <FileInput
                  label="Image de référence"
                  value={image}
                  onChange={(f) => setImage(f as File | null)}
                  accept="image/png,image/jpeg,image/webp"
                  maxSize={15 * 1024 * 1024}
                  mode="dropzone"
                  description="PNG, JPEG ou WebP — 15 Mo max."
                  isDisabled={isBusy}
                  isRequired
                />
                <TextArea
                  label="Décris la scène"
                  value={prompt}
                  onChange={setPrompt}
                  placeholder="Ex : la personne sur la photo lève la main et sourit, caméra fixe."
                  maxLength={2000}
                  isDisabled={isBusy}
                  isRequired
                />
                <Selector
                  label="Durée"
                  value={duration}
                  onChange={setDuration}
                  options={DURATIONS.map((d) => ({ label: `${d}s`, value: d }))}
                  isDisabled={isBusy}
                />
                <Button
                  label="Générer"
                  variant="primary"
                  onClick={generate}
                  isDisabled={!image || !prompt.trim() || isBusy}
                  isLoading={isBusy}
                />
              </VStack>
            </Card>

            {status !== "idle" && (
              <Card>
                <VStack gap={4}>
                  <HStack gap={2} align="center">
                    <StatusDot
                      variant={status === "done" ? "success" : status === "error" ? "error" : "accent"}
                      label={status}
                    />
                    <Text>
                      {status === "pending" && "En file d'attente…"}
                      {status === "running" && "Génération en cours…"}
                      {status === "done" && "Vidéo prête."}
                      {status === "error" && "Échec de la génération."}
                    </Text>
                  </HStack>
                  {isBusy && <ProgressBar label="Progression" isIndeterminate variant="accent" />}
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
                  Historique (3 dernières)
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
                          label={h.status}
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
          )}
        </LayoutContent>
      }
    />
  );
}
