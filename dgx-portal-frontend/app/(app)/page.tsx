"use client";

import { useCallback, useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Button } from "@astryxdesign/core/Button";
import { useToast } from "@astryxdesign/core/Toast";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import {
  KeyIcon,
  MagnifyingGlassIcon,
  PaperAirplaneIcon,
  MoonIcon,
  CpuChipIcon,
  CircleStackIcon,
  BoltIcon,
  DocumentMagnifyingGlassIcon,
  FilmIcon,
  SpeakerWaveIcon,
  PhotoIcon,
  MusicalNoteIcon,
  ExclamationTriangleIcon,
  ChatBubbleLeftRightIcon,
} from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { fetchConversations, relativeTime } from "@/lib/conversations";
import type { Conversation } from "@/lib/types";
import { useWhoami } from "@/lib/whoami";
import { UsageChart } from "./_components/UsageChart";
import { useT } from "@/lib/i18n";
import { useSettingsDialog } from "@/lib/settings-dialog";

type SysMetrics = {
  cpu_pct: number;
  ram: { used_gb: number; total_gb: number; pct: number };
  gpu?: { util: number; power: number; temp: number };
} | null;

type ModelHealth = {
  model: string | null;
  up: boolean;
  tps: number | null;
  running: number;
  waiting: number;
  ttft: number | null;
  requests: number | null;
  max_seqs: number | null;
  ctx_in: number | null;
  ctx_out: number | null;
} | null;

interface ModelRequest extends Record<string, unknown> {
  model_id: string;
  reason: string | null;
  status: string;
  created_at: string;
}

type RunningModel = { name: string; kind: "chat" | "image" | "music" | "video" | "ocr" | "voice"; exposed: boolean };

type SidecarMetric = {
  count_today: number;
  total: number;
  avg_ms: number | null;
  last_ms: number | null;
  chars_per_s?: number | null;   // ocr + voice: character throughput
  chars_avg?: number | null;      // ocr: density (characters / document)
  rtf?: number | null;            // voice: real-time factor (×N)
  success_rate?: number | null;   // video: success rate %
  video_secs_today?: number | null; // video: seconds of video generated today
  gen_per_vsec?: number | null;   // video: compute seconds per second of video
};

// Token count → compact format "192k", "256k" (base 1024, like the usual
// context sizes). Below 1024, we show the raw number.
function fmtCtx(n: number | null): string {
  if (n == null) return "—";
  return n >= 1024 ? `${Math.round(n / 1024)}k` : `${n}`;
}

// ms → readable duration: "850 ms", "4.2 s", "3 min 12 s".
function fmtDur(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s % 60).toString().padStart(2, "0")} s`;
}

// Metric rows [label, value] for a media backend, in order of interest.
function sidecarLines(
  kind: "ocr" | "video" | "voice",
  sm: SidecarMetric,
  t: (s: string) => string,
): [string, string][] {
  const num = (n: number) => n.toLocaleString();
  const lines: [string, string][] = [[t("Aujourd'hui"), num(sm.count_today)]];
  if (kind === "ocr") {
    if (sm.chars_per_s != null) lines.push([t("Débit"), `≈ ${num(sm.chars_per_s)} c/s`]);
    if (sm.chars_avg != null) lines.push([t("Caractères/doc"), num(sm.chars_avg)]);
    if (sm.avg_ms != null) lines.push([t("Extraction moy."), fmtDur(sm.avg_ms)]);
  } else if (kind === "voice") {
    if (sm.rtf != null) lines.push([t("Facteur temps réel"), `×${sm.rtf}`]);
    if (sm.chars_per_s != null) lines.push([t("Vitesse de synthèse"), `≈ ${num(sm.chars_per_s)} c/s`]);
    if (sm.avg_ms != null) lines.push([t("Génération moy."), fmtDur(sm.avg_ms)]);
  } else {
    if (sm.success_rate != null) lines.push([t("Taux de réussite"), `${sm.success_rate} %`]);
    if (sm.video_secs_today != null) lines.push([t("Vidéo générée auj."), `${num(sm.video_secs_today)} s`]);
    if (sm.gen_per_vsec != null) lines.push([t("Calcul / s de vidéo"), `${sm.gen_per_vsec}×`]);
    if (sm.avg_ms != null) lines.push([t("Génération moy."), fmtDur(sm.avg_ms)]);
  }
  if (sm.last_ms != null) lines.push([t("Dernière"), fmtDur(sm.last_ms)]);
  return lines;
}

// What each capability does, in one line (shown on each model card).
const KIND_DESC: Record<RunningModel["kind"], string> = {
  chat: "Chat & complétions — API OpenAI-compatible",
  image: "Génération d'images (texte → image)",
  music: "Génération musicale (texte → chanson)",
  video: "Génération de vidéos courtes (texte ou image → vidéo)",
  ocr: "Extraction de texte et de tableaux depuis images et PDF",
  voice: "Clonage de voix zéro-shot à partir d'un court échantillon",
};
// Destination + libellé du bouton « Ouvrir » sur les cartes de services média.
// Chaque capacité va sur SA page (le fallback « vidéo » était le bug : image et
// musique atterrissaient sur /video).
const KIND_OPEN: Record<RunningModel["kind"], { label: string; href: string }> = {
  chat: { label: "Ouvrir le chat", href: "/playground" },
  image: { label: "Ouvrir la génération d'image", href: "/image" },
  music: { label: "Ouvrir la génération musicale", href: "/music" },
  video: { label: "Ouvrir la génération vidéo", href: "/video" },
  ocr: { label: "Ouvrir l'OCR", href: "/ocr" },
  voice: { label: "Ouvrir le clonage de voix", href: "/voice" },
};
// Short name of the media backends (for the "Media services" block).
const KIND_NAME: Record<"ocr" | "video" | "voice", string> = { ocr: "OCR", video: "Vidéo", voice: "Voix" };

// Bandeau « disponibilité par capacité » : chaque capacité va sur SA page, et
// son statut reflète un modèle en cours d'exécution de ce type.
const CAPS: { kind: RunningModel["kind"]; label: string; icon: typeof PhotoIcon }[] = [
  { kind: "chat", label: "Chat", icon: ChatBubbleLeftRightIcon },
  { kind: "image", label: "Image", icon: PhotoIcon },
  { kind: "music", label: "Musique", icon: MusicalNoteIcon },
  { kind: "video", label: "Vidéo", icon: FilmIcon },
  { kind: "ocr", label: "OCR", icon: DocumentMagnifyingGlassIcon },
  { kind: "voice", label: "Voix", icon: SpeakerWaveIcon },
];

type HomeData = {
  running_models: RunningModel[];
  public_api_url: string;
  auto_model: string;
  sysmetrics: SysMetrics;
  sidecar_metrics: Partial<Record<"ocr" | "video" | "voice", SidecarMetric>>;
  modelhealth: ModelHealth;
  active_users: { username: string; requests: number; tokens: number; live?: boolean }[] | null;
  usage: { has_data: boolean; total: number; active_keys: number; points: { hour: number; tokens: number }[] } | null;
  usage_by_model: { model: string; tokens: number }[];
  my_requests: ModelRequest[];
  budget_tokens: string;
  budget_duration: string;
  budget_used: number;
  budget_remaining: number;
};


const STATUS_VARIANT: Record<string, "warning" | "success" | "error"> = {
  pending: "warning",
  done: "success",
  rejected: "error",
};
const STATUS_LABEL: Record<string, string> = { pending: "En attente", done: "Lancé", rejected: "Refusé" };

const buildRequestColumns = (t: (s: string) => string): TableColumn<ModelRequest>[] => [
  { key: "model_id", header: t("Modèle") },
  { key: "reason", header: t("Raison"), renderCell: (row) => row.reason || "—" },
  {
    key: "status",
    header: t("Statut"),
    renderCell: (row) => <Badge label={t(STATUS_LABEL[row.status] || row.status)} variant={STATUS_VARIANT[row.status] || "neutral"} />,
  },
  { key: "created_at", header: t("Date"), renderCell: (row) => row.created_at.slice(0, 16).replace("T", " ") },
];

export default function HomePage() {
  const t = useT();
  const { open: openSettings } = useSettingsDialog();
  const showToast = useToast();
  const [data, setData] = useState<HomeData | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [recentConvs, setRecentConvs] = useState<Conversation[]>([]);
  const { who } = useWhoami();

  // Reprendre une conversation : les plus récentes, pour ouvrir le playground
  // dessus. Chargées une fois au montage (aller-retour léger, sans csrf).
  useEffect(() => {
    let annule = false;
    fetchConversations()
      .then((c) => { if (!annule) setRecentConvs(c); })
      .catch(() => {});
    return () => { annule = true; };
  }, []);

  // Charge le tableau de bord. On ne signale l'erreur que s'il n'y a encore rien
  // à afficher (un échec de poll transitoire ne doit pas effacer les données déjà
  // affichées) ; un succès met à jour l'horodatage « dernière mise à jour ».
  const load = useCallback(() => {
    if (document.visibilityState !== "visible") return;
    getJSON<HomeData>("/api/home")
      .then((d) => {
        setData(d);
        setLoadError(false);
        setLastUpdated(Date.now());
      })
      .catch(() => setLoadError(true));
  }, []);

  // Le débit et le TTFT sont les seules valeurs qui bougent à la seconde. On les
  // rafraîchit sur un endpoint dédié et minuscule (/api/modelhealth) plutôt que de
  // passer tout le tableau de bord à 1 s : /api/home agrège les dépenses et sonde
  // les sidecars, le payer 5 fois plus souvent pour deux chiffres serait absurde.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      getJSON<ModelHealth>("/api/modelhealth")
        .then((h) => setData((d) => (d ? { ...d, modelhealth: h } : d)))
        .catch(() => {});   // poll transitoire : le cycle 5 s reprendra la main
    };
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // Après le round-trip Discord OAuth (ou unlink), le backend revient ici avec
  // ?discord=… — on affiche le résultat et on ouvre l'onglet qui héberge le
  // lien, puis on nettoie l'URL pour qu'un refresh ne rejoue pas la scène.
  useEffect(() => {
    const d = new URLSearchParams(window.location.search).get("discord");
    if (!d) return;
    if (d === "linked") {
      showToast({ body: t("Compte Discord lié — tu recevras les annonces en message privé."), type: "info" });
      openSettings("keys");
    } else if (d === "unlinked") {
      showToast({ body: t("Compte Discord délié."), type: "info" });
    } else if (d === "error") {
      showToast({ body: t("Discord : échec de la liaison. Réessaie."), type: "error" });
      openSettings("keys");
    } else if (d === "unavailable") {
      showToast({ body: t("La liaison Discord n'est pas configurée."), type: "error" });
    }
    window.history.replaceState({}, "", window.location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once on mount
  }, []);

  // Poll the dashboard every 5s so server load, running models and active
  // users stay live without a manual refresh. 5 s et pas moins : vllm_health()
  // met ses metriques en cache ~4 s, donc sonder plus vite ne rafraichirait
  // rien de plus et ne ferait que multiplier les requetes. L'agregat SpendLogs
  // derriere passe par l'index startTime (~0,15 ms), il encaisse ce rythme.
  // load() s'occupe du fetch initial (tick immédiat) ET du rythme, et saute le
  // round-trip quand l'onglet est masqué — pas de double requête au montage.
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    document.addEventListener("visibilitychange", load);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", load);
    };
  }, [load]);

  const firstName = who?.fullname?.split(" ")[0] || "";

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={6}>
            <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
              <VStack gap={1}>
                <Heading level={1}>{t("Bonjour")}{firstName ? `, ${firstName}` : ""}</Heading>
                <Text type="supporting" color="secondary">{t("Ton accès self-service à l'inférence LLM sur DGX Spark.")}</Text>
                <HStack gap={2} wrap="wrap">
                  <Button label={t("Explorer les modèles")} variant="ghost" size="sm" icon={<Icon icon={MagnifyingGlassIcon} size="sm" />} href="/search" />
                  <Button label={t("Demander un modèle")} variant="ghost" size="sm" icon={<Icon icon={PaperAirplaneIcon} size="sm" />} href="/request" />
                </HStack>
              </VStack>
              <HStack gap={2} wrap="wrap">
                <Button label={t("Lancer une conversation")} variant="primary" icon={<Icon icon={ChatBubbleLeftRightIcon} size="sm" />} href="/playground" />
                <Button label={t("Mes clés API")} variant="secondary" icon={<Icon icon={KeyIcon} size="sm" />} onClick={() => openSettings("keys")} />
              </HStack>
            </HStack>

            {data ? (
              <VStack gap={2}>
                <Text weight="semibold">{t("Disponibilité par capacité")}</Text>
                <Grid columns={{ minWidth: 150, max: 6 }} gap={3}>
                  {CAPS.map((c) => {
                    const on = data.running_models.some((m) => m.kind === c.kind);
                    return (
                      <ClickableCard
                        key={c.kind}
                        label={KIND_OPEN[c.kind].label}
                        variant="muted"
                        href={KIND_OPEN[c.kind].href}
                      >
                        <VStack gap={1}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={c.icon} size="sm" />
                            <Text weight="semibold" size="sm">{t(c.label)}</Text>
                          </HStack>
                          <Badge label={on ? t("En ligne") : t("à la demande")} variant={on ? "success" : "neutral"} />
                        </VStack>
                      </ClickableCard>
                    );
                  })}
                </Grid>
              </VStack>
            ) : null}

            <VStack gap={2}>
              <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                <Text weight="semibold">{t("Modèles disponibles maintenant")}</Text>
                {lastUpdated ? (
                  <Text type="supporting" color="secondary">
                    {t("Mis à jour")} · {new Date(lastUpdated).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                  </Text>
                ) : null}
              </HStack>
              {data === null ? (
                loadError ? (
                  <Card>
                    <VStack gap={2}>
                      <HStack gap={2} vAlign="center">
                        <Icon icon={ExclamationTriangleIcon} size="sm" />
                        <Text weight="semibold">{t("Impossible de charger le tableau de bord")}</Text>
                      </HStack>
                      <Text type="supporting" color="secondary">
                        {t("Le serveur n'a pas répondu. Réessaie dans un instant.")}
                      </Text>
                      <HStack>
                        <Button label={t("Réessayer")} variant="secondary" size="sm" onClick={load} />
                      </HStack>
                    </VStack>
                  </Card>
                ) : (
                  <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                    {[0, 1, 2].map((i) => (
                      <Card key={i}>
                        <VStack gap={2}>
                          <Skeleton height={16} width={140} />
                          <Skeleton height={14} width={200} />
                        </VStack>
                      </Card>
                    ))}
                  </Grid>
                )
              ) : data.running_models.length === 0 ? (
                <EmptyState
                  icon={<Icon icon={MoonIcon} size="lg" />}
                  title={t("Aucun modèle actif")}
                  description={t("Demande le lancement d'un modèle.")}
                  actions={<Button label={t("Demander un modèle")} variant="secondary" href="/request" />}
                  isCompact
                />
              ) : (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.running_models.map((m) => (
                    <Card key={m.name}>
                      <VStack gap={2} height="100%">
                        <HStack hAlign="between" vAlign="center">
                          <Badge label={t("En ligne")} variant="success" />
                          {m.kind === "ocr" && <Icon icon={DocumentMagnifyingGlassIcon} size="sm" />}
                          {m.kind === "video" && <Icon icon={FilmIcon} size="sm" />}
                          {m.kind === "voice" && <Icon icon={SpeakerWaveIcon} size="sm" />}
                          {m.kind === "image" && <Icon icon={PhotoIcon} size="sm" />}
                          {m.kind === "music" && <Icon icon={MusicalNoteIcon} size="sm" />}
                        </HStack>
                        <Text weight="semibold" wordBreak="break-all">
                          {m.name}
                        </Text>
                        <Text type="supporting" color="secondary">{t(KIND_DESC[m.kind])}</Text>
                        {m.exposed ? (
                          <>
                            <Text type="supporting" color="secondary">
                              {t("API :")} {data.public_api_url}
                            </Text>
                            {data.auto_model && (
                              <Text type="supporting" color="secondary">
                                {t("Astuce : appelle « {model} » comme nom de modèle pour toujours cibler le modèle en cours — sans changer ton code à chaque bascule.").replace("{model}", data.auto_model)}
                              </Text>
                            )}
                            <StackItem size="fill" />
                            <Button
                              label={t("Créer une clé API")}
                              variant="secondary"
                              size="sm"
                              onClick={() => openSettings("keys")}
                            />
                          </>
                        ) : (
                          <>
                            <Text type="supporting" color="secondary">
                              {t("Disponible depuis l'application, non exposé par l'API.")}
                            </Text>
                            <StackItem size="fill" />
                            <Button
                              label={t(KIND_OPEN[m.kind].label)}
                              variant="secondary"
                              size="sm"
                              href={KIND_OPEN[m.kind].href}
                            />
                          </>
                        )}
                      </VStack>
                    </Card>
                  ))}
                </Grid>
              )}
            </VStack>

            {recentConvs.length > 0 && (
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center">
                  <Text weight="semibold">{t("Reprendre une conversation")}</Text>
                  <Button label={t("Tout voir")} variant="ghost" size="sm" href="/playground" />
                </HStack>
                <Grid columns={{ minWidth: 260, max: 3 }} gap={3}>
                  {recentConvs.slice(0, 3).map((c) => (
                    <ClickableCard
                      key={c.id}
                      label={c.title || t("Conversation")}
                      variant="muted"
                      href={`/playground?conv=${encodeURIComponent(c.id)}`}
                    >
                      <VStack gap={0}>
                        <Text maxLines={1} weight="semibold">{c.title || t("Conversation")}</Text>
                        <Text type="supporting" color="secondary">
                          {c.model || ""}{c.model ? " · " : ""}{relativeTime(c.ts)}
                        </Text>
                      </VStack>
                    </ClickableCard>
                  ))}
                </Grid>
              </VStack>
            )}

            {data?.sysmetrics && (
              <VStack gap={2}>
                <Text weight="semibold">{t("État du serveur")}</Text>
                <Card>
                  <VStack gap={4}>
                    <Grid columns={{ minWidth: 220, max: 3 }} gap={4}>
                      <VStack gap={1}>
                        <HStack hAlign="between">
                          <HStack gap={1} vAlign="center">
                            <Icon icon={CpuChipIcon} size="sm" />
                            <Text type="supporting" color="secondary">{t("CPU")}</Text>
                          </HStack>
                          <Text hasTabularNumbers>{data.sysmetrics.cpu_pct} %</Text>
                        </HStack>
                        <ProgressBar label={t("CPU")} isLabelHidden value={data.sysmetrics.cpu_pct} />
                      </VStack>
                      <VStack gap={1}>
                        <HStack hAlign="between">
                          <HStack gap={1} vAlign="center">
                            <Icon icon={CircleStackIcon} size="sm" />
                            <Text type="supporting" color="secondary">{t("RAM")}</Text>
                          </HStack>
                          <Text hasTabularNumbers>
                            {data.sysmetrics.ram.used_gb} / {data.sysmetrics.ram.total_gb} {t("Go")}
                          </Text>
                        </HStack>
                        <ProgressBar label={t("RAM")} isLabelHidden value={data.sysmetrics.ram.pct} />
                      </VStack>
                      {data.sysmetrics.gpu && (
                        <VStack gap={1}>
                          <HStack hAlign="between">
                            <HStack gap={1} vAlign="center">
                              <Icon icon={BoltIcon} size="sm" />
                              <Text type="supporting" color="secondary">{t("GPU")}</Text>
                            </HStack>
                            <Text hasTabularNumbers>
                              {Math.round(data.sysmetrics.gpu.util)} % · {Math.round(data.sysmetrics.gpu.power)} W ·{" "}
                              {Math.round(data.sysmetrics.gpu.temp)} °C
                            </Text>
                          </HStack>
                          <ProgressBar label={t("GPU")} isLabelHidden value={data.sysmetrics.gpu.util} />
                        </VStack>
                      )}
                    </Grid>

                    {data.modelhealth && (
                      <HStack gap={5} wrap="wrap">
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Modèle actif")}</Text>
                          <HStack gap={2} vAlign="center">
                            <Text weight="semibold">{data.modelhealth.model || t("aucun")}</Text>
                            <Badge label={data.modelhealth.up ? t("en ligne") : t("arrêté")} variant={data.modelhealth.up ? "success" : "neutral"} />
                          </HStack>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Débit")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.tps ?? "—"} tok/s</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Sessions")}</Text>
                          <Text weight="semibold" hasTabularNumbers>
                            {data.modelhealth.running} / {data.modelhealth.max_seqs ?? "—"}
                          </Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("TTFT")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.ttft ?? "—"} s</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Requêtes servies")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.requests ?? "—"}</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Contexte entrée")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{fmtCtx(data.modelhealth.ctx_in)}</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Contexte sortie")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{fmtCtx(data.modelhealth.ctx_out)}</Text>
                        </VStack>
                        {data.modelhealth.model && (
                          <VStack gap={1} hAlign="start">
                            <Text type="supporting" color="secondary">{t("Accès rapide")}</Text>
                            <Button
                              label={t("Discuter avec le modèle actif")}
                              variant="secondary"
                              size="sm"
                              icon={<Icon icon={ChatBubbleLeftRightIcon} size="sm" />}
                              href={`/playground?model=${encodeURIComponent(data.modelhealth.model)}`}
                            />
                          </VStack>
                        )}
                      </HStack>
                    )}

                    {data.sidecar_metrics && (["ocr", "video", "voice"] as const).some((k) => data.sidecar_metrics[k]) && (
                      <VStack gap={3}>
                        <Text type="supporting" color="secondary">{t("Services média")}</Text>
                        {(["ocr", "video", "voice"] as const)
                          .filter((k) => data.sidecar_metrics[k])
                          .map((k) => (
                            <VStack key={k} gap={2}>
                              <HStack gap={1} vAlign="center">
                                <Icon icon={k === "ocr" ? DocumentMagnifyingGlassIcon : k === "video" ? FilmIcon : SpeakerWaveIcon} size="sm" />
                                <Text weight="semibold">{t(KIND_NAME[k])}</Text>
                              </HStack>
                              <HStack gap={5} wrap="wrap">
                                {sidecarLines(k, data.sidecar_metrics[k]!, t).map(([label, value]) => (
                                  <VStack key={label} gap={0}>
                                    <Text type="supporting" color="secondary">{label}</Text>
                                    <Text weight="semibold" hasTabularNumbers>{value}</Text>
                                  </VStack>
                                ))}
                              </HStack>
                            </VStack>
                          ))}
                      </VStack>
                    )}

                    {who?.is_admin && data.active_users && (
                      <VStack gap={2}>
                        <Text type="supporting" color="secondary">{t("Qui utilise le modèle · 2 dernières min · visible admin uniquement")}</Text>
                        {data.active_users.length === 0 ? (
                          <Text type="supporting" color="secondary">{t("Personne n'utilise le modèle en ce moment.")}</Text>
                        ) : (
                          <HStack gap={2} wrap="wrap">
                            {data.active_users.map((u) => (
                              <Badge
                                key={u.username}
                                variant={u.live ? "success" : "neutral"}
                                label={u.live
                                  ? `${u.username} · ${t("en direct")}`
                                  : `${u.username} · ${u.requests} req · ${Math.round(u.tokens).toLocaleString("fr-FR")} tok`}
                              />
                            ))}
                          </HStack>
                        )}
                      </VStack>
                    )}
                  </VStack>
                </Card>
              </VStack>
            )}

            {data?.usage?.has_data && (
              <VStack gap={2}>
                <Text weight="semibold">{t("Mon utilisation — aujourd'hui")}</Text>
                <Card>
                  <VStack gap={3}>
                    <Grid columns={3} gap={2}>
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{t("Tokens · 24 h")}</Text>
                        <Text size="xl" weight="bold" hasTabularNumbers>
                          {Math.round(data.usage.total).toLocaleString("fr-FR")}
                        </Text>
                      </VStack>
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{t("Clés actives")}</Text>
                        <Text size="xl" weight="bold" hasTabularNumbers>
                          {data.usage.active_keys}
                        </Text>
                      </VStack>
                    </Grid>
                    <UsageChart points={data.usage.points} />
                  </VStack>
                </Card>
              </VStack>
            )}

            <Grid columns={{ minWidth: 260, max: 3 }} gap={3}>
              <Card>
                <VStack gap={2} height="100%">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={KeyIcon} size="sm" />
                    <Text weight="semibold">{t("Mes clés API")}</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">{t("Crée des clés personnelles pour accéder aux modèles via l'API OpenAI-compatible.")}</Text>
                  <Text type="supporting" color="secondary">
                    {t("Limite :")} {who?.is_admin ? t("Illimitée (admin)") : `${data?.budget_tokens ?? "—"} tokens / ${data?.budget_duration ?? "—"}`}
                  </Text>
                  {!who?.is_admin && data && (
                    <VStack gap={2}>
                      <ProgressBar
                        label={t("Quota consommé")}
                        value={data.budget_used}
                        max={(data.budget_used + data.budget_remaining) || 1}
                        variant={data.budget_remaining <= 0 ? "error" : "accent"}
                        hasValueLabel
                        formatValueLabel={(v, m) => `${Math.round((v / m) * 100)} %`}
                      />
                      <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                        <Text type="supporting" color="secondary">
                          {t("Restant :")} <Text hasTabularNumbers weight="semibold">{data.budget_remaining.toLocaleString("fr-FR")}</Text> {t("tokens")}
                        </Text>
                        <Button
                          label={t("Demander plus de budget")}
                          variant="secondary"
                          size="sm"
                          href="/request"
                        />
                      </HStack>
                    </VStack>
                  )}
                  <StackItem size="fill" />
                  <Button label={t("Gérer mes clés")} variant="secondary" onClick={() => openSettings("keys")} />
                </VStack>
              </Card>
              {(data?.usage_by_model?.length ?? 0) > 0 && (
                <Card>
                  <VStack gap={2} height="100%">
                    <HStack gap={2} vAlign="center">
                      <Icon icon={CpuChipIcon} size="sm" />
                      <Text weight="semibold">{t("Usage par modèle")}</Text>
                    </HStack>
                    <Text type="supporting" color="secondary">{t("Tokens consommés ces dernières 24 h, par modèle (tous comptes).")}</Text>
                    <VStack gap={2}>
                      {data!.usage_by_model.slice(0, 6).map((m) => (
                        <HStack key={m.model} hAlign="between" vAlign="center" gap={2}>
                          <Text type="supporting" maxLines={1}>{m.model}</Text>
                          <Text hasTabularNumbers weight="semibold">{m.tokens.toLocaleString("fr-FR")} {t("tokens")}</Text>
                        </HStack>
                      ))}
                    </VStack>
                  </VStack>
                </Card>
              )}
              <Card>
                <VStack gap={2} height="100%">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={MagnifyingGlassIcon} size="sm" />
                    <Text weight="semibold">{t("Catalogue HuggingFace")}</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">{t("Parcours les modèles disponibles et demande le lancement de celui qui t'intéresse.")}</Text>
                  <StackItem size="fill" />
                  <Button label={t("Explorer les modèles")} variant="secondary" href="/search" />
                </VStack>
              </Card>
              <Card>
                <VStack gap={2} height="100%">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={PaperAirplaneIcon} size="sm" />
                    <Text weight="semibold">{t("Demander un modèle")}</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">{t("Tu connais un modèle que tu veux tester ? Envoie une demande à l'admin.")}</Text>
                  <StackItem size="fill" />
                  <Button label={t("Faire une demande")} variant="secondary" href="/request" />
                </VStack>
              </Card>
            </Grid>

            {data && (
              <VStack gap={2}>
                <Text weight="semibold">{t("Mes dernières demandes")}</Text>
                {data.my_requests.length > 0 ? (
                  <Card padding={0}>
                    <Table<ModelRequest> data={data.my_requests} columns={buildRequestColumns(t)} idKey="model_id" density="balanced" dividers="rows" />
                  </Card>
                ) : (
                  <Card padding={4}>
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">
                        {t("Aucune demande de modèle pour l'instant.")}
                      </Text>
                      <HStack>
                        <Button label={t("Demander un modèle")} variant="secondary" size="sm" href="/request" />
                      </HStack>
                    </VStack>
                  </Card>
                )}
              </VStack>
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
