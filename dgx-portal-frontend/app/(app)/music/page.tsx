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
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
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
import { ModelRequestButton } from "../_components/ModelRequestButton";

type JobStatus = "idle" | "running" | "done" | "error";
type HistoryItem = { job_id: string; prompt: string; lyrics: string | null; duration_s: number; status: string; count?: number; done_count?: number; created_at: string };
type RunningModel = { name: string; kind: string; exposed: boolean };

const STATUS_LABEL: Record<string, string> = {
  running: "Composition en cours…",
  done: "Morceau prêt.",
  error: "Échec de la génération.",
};
const STATUS_SHORT: Record<string, string> = { running: "En cours", done: "Terminé", error: "Erreur" };

const VERSIONS = [1, 2, 3];

/* Bruit déterministe (même hachage sinusoïdal que la page Voix) : serveur et
   client doivent tirer exactement les mêmes valeurs, sinon l'hydratation React
   diverge — d'où ceci plutôt que Math.random(). */
function noise(i: number, seed: number) {
  const x = Math.sin(i * 12.9898 + seed) * 43758.5453;
  return x - Math.floor(x);
}
/* Chaque barre a son amplitude ET sa durée propres : des durées différentes
   font dériver les barres les unes par rapport aux autres, donc le motif ne se
   répète jamais (une durée unique donnerait une vague mécanique). */
const WAVE_BARS = Array.from({ length: 48 }, (_, i) => ({
  amp: 0.3 + noise(i, 1) * 0.7,
  dur: 0.85 + noise(i, 2) * 1.1,
  delay: -noise(i, 3) * 2,
}));

export default function MusicPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [prompt, setPrompt] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [duration, setDuration] = useState(60);
  const [versions, setVersions] = useState(1);
  const [jobCount, setJobCount] = useState(1);   // versions demandées pour le job affiché
  const [doneCount, setDoneCount] = useState(0); // versions déjà produites
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

  function downloadTrack(id: string, idx = 0) {
    const a = document.createElement("a");
    a.href = `/music/file/${id}/${idx}`;
    a.download = `musique-${id}-${idx + 1}.wav`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function generate() {
    if (!prompt.trim() || !csrf) return;
    setStatus("running");
    setJobId(null);
    setJobCount(versions);
    setDoneCount(0);
    try {
      const res = await postFormData<{ job_id?: string; count?: number; error?: string }>(
        "/api/music/generate", csrf,
        { prompt, lyrics, duration: String(duration), count: String(versions) },
      );
      if (!res.job_id) {
        showToast({ body: res.error ? t(res.error) : t("Échec de la génération."), type: "error" });
        setStatus("error");
        return;
      }
      setJobId(res.job_id);
      setJobCount(res.count ?? versions);
      loadHistory();
      // La composition peut durer plusieurs minutes : on interroge le statut
      // plutôt que de tenir une requête HTTP ouverte tout du long.
      pollRef.current = setInterval(async () => {
        const r = await fetch(`/api/music/status/${res.job_id}`, { credentials: "include" });
        const st = await r.json();
        if (typeof st.count === "number") setJobCount(st.count);
        if (typeof st.done_count === "number") setDoneCount(st.done_count);
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
    setJobCount(item.count ?? 1);
    setDoneCount(item.done_count ?? (item.status === "done" ? (item.count ?? 1) : 0));
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
                    <VStack gap={4}>
                      <HStack gap={3} vAlign="center">
                        <Icon icon={MoonIcon} size="md" color="secondary" />
                        <VStack gap={0}>
                          <Text weight="semibold">{t("Aucun modèle musique chargé")}</Text>
                          <Text type="supporting" color="secondary">
                            {t("La génération est indisponible pour l'instant, mais tu peux réécouter tes morceaux précédents ci-dessous.")}
                          </Text>
                        </VStack>
                      </HStack>
                      <ModelRequestButton category="music" />
                    </VStack>
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
                      <HStack hAlign="between" vAlign="center" gap={2} wrap="wrap">
                        <Text type="supporting" color="secondary">{t("Nombre de versions")}</Text>
                        <SegmentedControl
                          label={t("Nombre de versions")}
                          value={String(versions)}
                          onChange={(v) => setVersions(Number(v) || 1)}
                        >
                          {VERSIONS.map((n) => (
                            <SegmentedControlItem key={n} value={String(n)} label={String(n)} isDisabled={isBusy} />
                          ))}
                        </SegmentedControl>
                      </HStack>
                      <Button
                        label={versions > 1 ? t("Composer {n} versions").replace("{n}", String(versions)) : t("Composer")}
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
                        <VStack gap={2}>
                          {/* Onde animée : purement décorative (le texte au-dessus
                              porte l'information), d'où aria-hidden. */}
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
                          <Text type="supporting" color="secondary">
                            {jobCount > 1
                              ? t("Version {d} sur {n} — tu peux quitter la page, la composition continue.")
                                  .replace("{d}", String(Math.min(doneCount + 1, jobCount)))
                                  .replace("{n}", String(jobCount))
                              : t("La composition d'un morceau prend plusieurs minutes — tu peux quitter la page, elle reste en cours.")}
                          </Text>
                        </VStack>
                      )}
                      {/* Les versions terminées s'écoutent sans attendre la fin du lot. */}
                      {jobId && doneCount > 0 && (
                        <VStack gap={3}>
                          {Array.from({ length: doneCount }).map((_, idx) => (
                            <VStack key={idx} gap={1}>
                              {jobCount > 1 && (
                                <Text type="supporting" color="secondary">
                                  {t("Version")} {idx + 1}
                                </Text>
                              )}
                              <audio src={`/music/file/${jobId}/${idx}`} controls style={{ width: "100%" }} />
                              <HStack>
                                <Button
                                  label={t("Télécharger")}
                                  variant="secondary"
                                  size="sm"
                                  icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                                  onClick={() => downloadTrack(jobId, idx)}
                                />
                              </HStack>
                            </VStack>
                          ))}
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
                          description={
                            `${new Date(h.created_at).toLocaleString("fr-FR")} · ${h.duration_s}s` +
                            ((h.count ?? 1) > 1 ? ` · ${h.count} ${t("versions")}` : "")
                          }
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
