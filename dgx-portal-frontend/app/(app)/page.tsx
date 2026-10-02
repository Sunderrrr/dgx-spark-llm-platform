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
import { authFetch, getJSON } from "@/lib/api";
import { useCsrf } from "@/lib/useCsrf";
import { fetchConversations, relativeTime } from "@/lib/conversations";
import type { Conversation } from "@/lib/types";
import { useWhoami } from "@/lib/whoami";
import { UserAvatar } from "@/lib/user-avatar";
import { UsageChart } from "./_components/UsageChart";
import { useT, useLocale } from "@/lib/i18n";
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
  // Cumulative engine counters. Absent when the engine is stopped or its
  // metrics are unreachable: the display then falls back on « — », never on a
  // zero that would suggest a counter reset.
  tokens_generated?: number | null;
  tokens_generated_total?: number | null;
  tps_moyen?: number | null;
  tokens_prompt?: number | null;
  tps_prefill?: number | null;
  // In-flight activity seen by the ENGINE (llama.cpp /slots). Absent for vLLM,
  // which does not expose /slots: the absence must not read as « personne ne travaille ».
  slots?: {
    busy: number;
    total: number;
    plus_ancien_s: number | null;
    prompt_ingere: number;
    prompt_traite: number;
  } | null;
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

// How long since this user last asked the model for anything? The admin panel
// answers « qui utilise le modèle en temps réel »: a name without an age does
// not say it (« live » lights up for everyone as soon as the engine works),
// whereas « il y a 4 s » versus « il y a 41 min » reads at a glance.
function ageDepuis(secondes: number | null | undefined, t: (s: string) => string): string {
  if (secondes == null) return t("activité inconnue");
  if (secondes < 5) return t("à l'instant");
  if (secondes < 60) return t("il y a {n} s").replace("{n}", String(Math.round(secondes)));
  if (secondes < 3600) return t("il y a {n} min").replace("{n}", String(Math.round(secondes / 60)));
  return t("il y a {n} h").replace("{n}", String(Math.round(secondes / 3600)));
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
  numLocale: string,
): [string, string][] {
  // The locale follows the displayed language; passed by the component because
  // a helper outside a component cannot call a hook.
  const num = (n: number) => n.toLocaleString(numLocale);
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
// Destination + label of the « Ouvrir » button on the media service cards.
// Each capability goes to ITS own page (the « vidéo » fallback was the bug:
// image and music landed on /video).
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

// « disponibilité par capacité » banner: each capability goes to ITS own
// page, and its status reflects a running model of that type.
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
  // `en_vol` comes from the LiteLLM callback: it is the only OBSERVED
  // attribution during a request (the others rest on an inference from the engine).
  active_users: { username: string; requests: number; tokens: number; live?: boolean; derniere_s?: number | null; en_vol?: boolean; depuis_s?: number | null }[] | null;
  usage: { has_data: boolean; total: number; active_keys: number; points: { hour: number; tokens: number }[] } | null;
  usage_by_model: { model: string; tokens: number }[];
  my_requests: ModelRequest[];
  budget_tokens: string;
  budget_duration: string;
  budget_used: number;
  budget_remaining: number;
  budget_reset_at: string;
  budget_request_pending: boolean;
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
  const numLocale = useLocale();
  const { open: openSettings } = useSettingsDialog();
  const showToast = useToast();
  const csrf = useCsrf();
  const [data, setData] = useState<HomeData | null>(null);
  // Session in progress seen by the engine (refreshed every second with the
  // rest of modelhealth): used to tell « rien ne tourne » from « ça tourne mais
  // aucune identité n'est encore journalisée ».
  const slots = data?.modelhealth?.slots ?? null;
  const [loadError, setLoadError] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [recentConvs, setRecentConvs] = useState<Conversation[]>([]);
  const { who } = useWhoami();

  // Resume a conversation: the most recent ones, to open the playground on
  // them. Loaded once on mount (light round-trip, no csrf).
  useEffect(() => {
    let annule = false;
    fetchConversations()
      .then((c) => { if (!annule) setRecentConvs(c); })
      .catch(() => {});
    return () => { annule = true; };
  }, []);

  // Loads the dashboard. We only report the error if there is still nothing
  // to display (a transient poll failure must not erase data already
  // displayed); a success updates the « dernière mise à jour » timestamp.
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

  // Throughput and TTFT are the only values that move by the second. We refresh
  // them on a tiny dedicated endpoint (/api/modelhealth) rather than pushing
  // the whole dashboard to 1 s: /api/home aggregates spend and probes the
  // sidecars, paying it 5 times more often for two figures would be absurd.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      getJSON<ModelHealth>("/api/modelhealth")
        .then((h) => setData((d) => (d ? { ...d, modelhealth: h } : d)))
        .catch(() => {});   // transient poll: the 5 s cycle will take over
    };
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  // After the Discord OAuth (or unlink) round-trip, the backend comes back here
  // with ?discord=… — we display the result and open the tab hosting the
  // link, then clean the URL so a refresh does not replay the scene.
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
  // users stay live without a manual refresh. 5 s and no less: vllm_health()
  // caches its metrics for ~4 s, so polling faster would refresh nothing
  // more and only multiply the requests. The SpendLogs aggregate behind it
  // goes through the startTime index (~0.15 ms), it absorbs this pace.
  // load() handles the initial fetch (immediate tick) AND the pace, and skips
  // the round-trip when the tab is hidden — no double request on mount.
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
                {/* The avatar right next to the greeting: it is where you
                    recognize yourself on arrival, and at 48 px the blobatar moves
                    readably (at 24 px, the breathing is only guessed). */}
                <HStack gap={3} vAlign="center" wrap="wrap">
                  <Heading level={1}>{t("Bonjour")}{firstName ? `, ${firstName}` : ""}</Heading>
                  <UserAvatar avatarId={who?.avatar_id} username={who?.username} name={who?.fullname} size="lg" />
                </HStack>
                <Text type="supporting" color="secondary">{t("Ton accès self-service à l'inférence LLM sur DGX Spark.")}</Text>
                {/* « Explorer les modèles » / « Demander un modèle » buttons
                    removed: already in cards further down this page. */}
              </VStack>
              <HStack gap={2} wrap="wrap">
                {/* « Lancer une conversation » removed: the quick access is
                    already in the sidebar. */}
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
                        label={t(KIND_OPEN[c.kind].label)}
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
                            {/* « Créer une clé API » removed: already at the top
                                right of the page. */}
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
                          {c.model || ""}{c.model ? " · " : ""}{relativeTime(c.ts, t)}
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
                          <Text type="supporting" color="secondary">{t("Tokens générés")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.tps ?? "—"} tok/s</Text>
                          {/* INSTANTANEOUS throughput, so it drops to 0 as soon as the
                              engine ingests a long input context: 4 requests in flight
                              and a single decode step in 6 s, measured on 14/09.
                              The average since startup gives the missing reference,
                              and it is exact (ratio of two engine counters, not a
                              portal sampling). */}
                          {data.modelhealth.tps_moyen != null && (
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {t("moyenne {n} tok/s").replace("{n}", String(data.modelhealth.tps_moyen))}
                            </Text>
                          )}
                        </VStack>
                        {/* The counterpart of the generation throughput, on the INPUT
                            side. An agentic client re-reads huge contexts: the engine
                            spends most of its time in prefill, so this figure
                            explains the 0 tok/s displayed on the left. */}
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Prefill")}</Text>
                          <Text weight="semibold" hasTabularNumbers>
                            {data.modelhealth.tps_prefill != null
                              ? `${data.modelhealth.tps_prefill} tok/s` : "—"}
                          </Text>
                          {data.modelhealth.tokens_prompt != null && (
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {t("{n} tokens d'entrée").replace(
                                "{n}", data.modelhealth.tokens_prompt.toLocaleString(numLocale))}
                            </Text>
                          )}
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
                        {/* Quick access removed: already covered by the
                            buttons at the top of the page. */}
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
                                {sidecarLines(k, data.sidecar_metrics[k]!, t, numLocale).map(([label, value]) => (
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
                        <HStack gap={2} wrap="wrap">
                          <Text type="supporting" color="secondary">{t("Qui utilise le modèle · 30 dernières minutes · visible admin uniquement")}</Text>
                          {/* GLOBAL engine throughput, never per person: the admin sees
                              WHO works here, and the speed at which the machine
                              serves everyone at once. */}
                          {data.modelhealth && (
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {t("débit global {n} tok/s").replace("{n}", String(data.modelhealth.tps ?? "—"))}
                              {data.modelhealth.tps_moyen != null
                                ? " · " + t("moyenne {n} tok/s").replace("{n}", String(data.modelhealth.tps_moyen))
                                : ""}
                            </Text>
                          )}
                        </HStack>
                        {/* IN-FLIGHT activity. LiteLLM only writes its row at the end of
                            a request: measured on 14/09, 44 minutes without a write while
                            two sessions were working, so an empty panel that made it
                            look like the machine was free. The engine, for its part,
                            can describe these sessions — busy, since when, prompt progress. */}
                        {slots && slots.busy > 0 && (
                          <Text type="supporting" color="secondary" hasTabularNumbers>
                            {t("{n} session(s) en cours sur le moteur").replace("{n}", String(slots.busy))}
                            {slots.total ? ` / ${slots.total}` : ""}
                            {slots.plus_ancien_s != null
                              ? " · " + t("la plus ancienne depuis {d}").replace("{d}", ageDepuis(slots.plus_ancien_s, t))
                              : ""}
                            {slots.prompt_ingere > slots.prompt_traite
                              ? " · " + t("ingestion du prompt {a} / {b} tokens")
                                  .replace("{a}", slots.prompt_traite.toLocaleString(numLocale))
                                  .replace("{b}", slots.prompt_ingere.toLocaleString(numLocale))
                              : ""}
                          </Text>
                        )}
                        {data.active_users.length === 0 ? (
                          slots && slots.busy > 0 ? (
                            <Text type="supporting" color="secondary">
                              {t("Ces requêtes sont anonymes jusqu'à leur fin : LiteLLM n'écrit la ligne qu'une fois la requête terminée.")}
                            </Text>
                          ) : (
                            <Text type="supporting" color="secondary">{t("Personne n'utilise le modèle en ce moment.")}</Text>
                          )
                        ) : (
                          <HStack gap={2} wrap="wrap">
                            {data.active_users.map((u) => (
                              <Badge
                                key={u.username}
                                variant={u.live ? "success" : "neutral"}
                                // The age is what distinguishes « il génère là » from « il a
                                // fini il y a une heure »: a flag alone does not say it,
                                // and over 30 minutes the difference is huge.
                                label={[
                                  u.username,
                                  // OBSERVED attribution by LiteLLM (request in
                                  // flight) rather than inferred: we display its duration.
                                  u.en_vol
                                    ? t("en cours depuis {d}").replace("{d}", ageDepuis(u.depuis_s ?? 0, t))
                                    : ageDepuis(u.derniere_s, t),
                                  u.live ? t("en direct") : null,
                                  u.en_vol ? null : `${u.requests} ${t("req")}`,
                                  u.en_vol ? null : `${Math.round(u.tokens).toLocaleString(numLocale)} ${t("tok")}`,
                                ].filter(Boolean).join(" · ")}
                              />
                            ))}
                          </HStack>
                        )}
                        {slots && slots.busy > 0 && data.active_users.length > 0
                          && !data.active_users.some((u) => u.live) && (
                          <Text type="supporting" color="secondary">
                            {t("Aucun de ces comptes n'a émis à l'instant : les sessions en cours ne sont pas encore attribuées.")}
                          </Text>
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
                          {Math.round(data.usage.total).toLocaleString(numLocale)}
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
                    <Icon icon={CircleStackIcon} size="sm" />
                    <Text weight="semibold">{t("Usage des quotas")}</Text>
                  </HStack>
                  {data && (who?.is_admin ? (
                    /* Admin = unlimited: NO quota mechanics here (gauge,
                       percentage, remaining, reset date) — they mean nothing
                       without an envelope and suggested a fictitious cap. */
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">{t("Limite :")} {t("Illimitée (admin)")}</Text>
                      <Text type="supporting" color="secondary">
                        {t("Utilisé :")} <Text hasTabularNumbers weight="semibold">{data.budget_used.toLocaleString(numLocale)}</Text> {t("tokens")}
                      </Text>
                    </VStack>
                  ) : (
                    <VStack gap={2}>
                      <HStack gap={2} vAlign="center" wrap="wrap">
                        <Text type="supporting" color="secondary">
                          {t("Ta consommation de tokens sur la fenêtre de budget « {duration} ».").replace("{duration}", data.budget_duration)}
                        </Text>
                        {data.budget_remaining <= 0 && <Badge label={t("Quota dépassé")} variant="error" />}
                        {data.budget_remaining > 0 && data.budget_used > 0
                          && (data.budget_used / (data.budget_used + data.budget_remaining)) >= 0.85
                          && <Badge label={t("Presque épuisé")} variant="warning" />}
                      </HStack>
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
                          {t("Utilisé :")} <Text hasTabularNumbers weight="semibold">{data.budget_used.toLocaleString(numLocale)}</Text> {t("tokens")}
                        </Text>
                        <Text type="supporting" color="secondary">
                          {t("Restant :")} <Text hasTabularNumbers weight="semibold">{data.budget_remaining.toLocaleString(numLocale)}</Text> {t("tokens")}
                        </Text>
                      </HStack>
                      {data.budget_reset_at && (
                        <Text type="supporting" color="secondary">
                          {t("Nouveau quota le {date} (UTC).").replace("{date}", data.budget_reset_at)}
                        </Text>
                      )}
                      <HStack hAlign="end">
                        {/* Real budget request (budget_requests + admin
                           notif), NOT a link to the « demande de modèle »
                           page: the first click sent people to request
                           an LLM model instead of tokens. */}
                        <Button
                          label={data.budget_request_pending ? t("Demande en cours d'examen") : t("Demander plus de budget")}
                          variant="secondary"
                          size="sm"
                          isDisabled={data.budget_request_pending}
                          onClick={async () => {
                            // The reason goes to the database and is displayed raw (column
                            // « Raison », admin queue): msgid translated client-side.
                            // The server's verdict is READ: `postForm` returns neither
                            // status nor body, so a refusal (request already
                            // pending, 409) showed up here as a successful send.
                            const raison = t("Demande depuis la page d'accueil (quota bientôt épuisé)");
                            try {
                              const res = await authFetch("/keys", {
                                method: "POST",
                                headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
                                body: new URLSearchParams({ action: "request_budget", reason: raison }).toString(),
                              });
                              const r = (await res.json().catch(() => ({}))) as { ok?: boolean; error?: string; code?: string };
                              if (!res.ok || r.ok === false) {
                                showToast({
                                  body: r.code ? t(r.code) : r.error ? t(r.error) : t("L'action a échoué."),
                                  type: "error",
                                });
                                return;
                              }
                              setData((d) => (d ? { ...d, budget_request_pending: true } : d));
                              showToast({ body: t("Demande envoyée à l'admin.") });
                            } catch {
                              showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
                            }
                          }}
                        />
                      </HStack>
                    </VStack>
                  ))}
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
                          <Text hasTabularNumbers weight="semibold">{m.tokens.toLocaleString(numLocale)} {t("tokens")}</Text>
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
              {/* « Demander un modèle » card removed: the
                  « Mes dernières demandes » section just below already
                  carries the button and the history. */}
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
