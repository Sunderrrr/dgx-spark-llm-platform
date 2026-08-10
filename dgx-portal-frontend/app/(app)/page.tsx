"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
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
} from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { UsageChart } from "./_components/UsageChart";
import { useT } from "@/lib/i18n";

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
  requests: number;
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

type RunningModel = { name: string; kind: "chat" | "ocr" | "video" | "voice"; exposed: boolean };

type SidecarMetric = {
  count_today: number;
  total: number;
  avg_ms: number | null;
  last_ms: number | null;
  chars_per_s?: number | null;   // ocr + voice : débit de caractères
  chars_avg?: number | null;      // ocr : densité (caractères / document)
  rtf?: number | null;            // voice : facteur temps réel (×N)
  success_rate?: number | null;   // video : % de réussite
  video_secs_today?: number | null; // video : secondes de vidéo générées aujourd'hui
  gen_per_vsec?: number | null;   // video : s de calcul par s de vidéo
};

// Nombre de tokens → format compact « 192k », « 256k » (base 1024, comme les
// tailles de contexte usuelles). En dessous de 1024, on affiche le nombre brut.
function fmtCtx(n: number | null): string {
  if (n == null) return "—";
  return n >= 1024 ? `${Math.round(n / 1024)}k` : `${n}`;
}

// ms → durée lisible : « 850 ms », « 4.2 s », « 3 min 12 s ».
function fmtDur(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s % 60).toString().padStart(2, "0")} s`;
}

// Lignes de métriques [label, valeur] d'un backend média, dans l'ordre d'intérêt.
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

// Ce que fait chaque capacité, en une ligne (affiché sur chaque carte modèle).
const KIND_DESC: Record<RunningModel["kind"], string> = {
  chat: "Chat & complétions — API OpenAI-compatible",
  ocr: "Extraction de texte et de tableaux depuis images et PDF",
  video: "Génération de vidéos courtes (texte ou image → vidéo)",
  voice: "Clonage de voix zéro-shot à partir d'un court échantillon",
};
// Nom court des backends média (pour le bloc « Services média »).
const KIND_NAME: Record<"ocr" | "video" | "voice", string> = { ocr: "OCR", video: "Vidéo", voice: "Voix" };

type HomeData = {
  running_models: RunningModel[];
  public_api_url: string;
  sysmetrics: SysMetrics;
  sidecar_metrics: Partial<Record<"ocr" | "video" | "voice", SidecarMetric>>;
  modelhealth: ModelHealth;
  active_users: { username: string; requests: number; tokens: number }[] | null;
  usage: { has_data: boolean; total: number; active_keys: number; points: { hour: number; tokens: number }[] } | null;
  my_requests: ModelRequest[];
  budget_tokens: string;
  budget_duration: string;
};

type Whoami = { username: string; fullname: string; is_admin: boolean };

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
  const [data, setData] = useState<HomeData | null>(null);
  const [who, setWho] = useState<Whoami | null>(null);

  useEffect(() => {
    getJSON<HomeData>("/api/home").then(setData);
    getJSON<Whoami>("/api/whoami").then(setWho);
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      getJSON<HomeData>("/api/home").then(setData).catch(() => {});
    }, 8000);
    return () => clearInterval(id);
  }, []);

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
              </VStack>
              <Button
                label={t("Mes clés API")}
                variant="primary"
                icon={<Icon icon={KeyIcon} size="sm" />}
                href="/keys"
              />
            </HStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Modèles disponibles maintenant")}</Text>
              {data && data.running_models.length === 0 && (
                <EmptyState
                  icon={<Icon icon={MoonIcon} size="lg" />}
                  title={t("Aucun modèle actif")}
                  description={t("Demande le lancement d'un modèle.")}
                  actions={<Button label={t("Demander un modèle")} variant="secondary" href="/request" />}
                  isCompact
                />
              )}
              {data && data.running_models.length > 0 && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.running_models.map((m) => (
                    <Card key={m.name}>
                      <VStack gap={2}>
                        <HStack hAlign="between" vAlign="center">
                          <Badge label={t("En ligne")} variant="success" />
                          {m.kind === "ocr" && <Icon icon={DocumentMagnifyingGlassIcon} size="sm" />}
                          {m.kind === "video" && <Icon icon={FilmIcon} size="sm" />}
                          {m.kind === "voice" && <Icon icon={SpeakerWaveIcon} size="sm" />}
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
                            <Button
                              label={t("Créer une clé API")}
                              variant="secondary"
                              size="sm"
                              href="/keys"
                            />
                          </>
                        ) : (
                          <>
                            <Text type="supporting" color="secondary">
                              {t("Disponible depuis l'application, non exposé par l'API.")}
                            </Text>
                            <Button
                              label={
                                m.kind === "ocr" ? t("Ouvrir l'OCR")
                                : m.kind === "voice" ? t("Ouvrir le clonage de voix")
                                : t("Ouvrir la génération vidéo")
                              }
                              variant="secondary"
                              size="sm"
                              href={m.kind === "ocr" ? "/ocr" : m.kind === "voice" ? "/voice" : "/video"}
                            />
                          </>
                        )}
                      </VStack>
                    </Card>
                  ))}
                </Grid>
              )}
            </VStack>

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
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.requests}</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Contexte entrée")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{fmtCtx(data.modelhealth.ctx_in)}</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Contexte sortie")}</Text>
                          <Text weight="semibold" hasTabularNumbers>{fmtCtx(data.modelhealth.ctx_out)}</Text>
                        </VStack>
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
                                variant="neutral"
                                label={`${u.username} · ${u.requests} req · ${Math.round(u.tokens).toLocaleString("fr-FR")} tok`}
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
                  <StackItem size="fill" />
                  <Button label={t("Gérer mes clés")} variant="secondary" href="/keys" />
                </VStack>
              </Card>
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

            {data && data.my_requests.length > 0 && (
              <VStack gap={2}>
                <Text weight="semibold">{t("Mes dernières demandes")}</Text>
                <Card padding={0}>
                  <Table<ModelRequest> data={data.my_requests} columns={buildRequestColumns(t)} idKey="model_id" density="balanced" dividers="rows" />
                </Card>
              </VStack>
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
