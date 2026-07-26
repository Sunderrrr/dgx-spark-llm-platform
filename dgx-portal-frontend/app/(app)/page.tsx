"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
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
} from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { UsageChart } from "./_components/UsageChart";

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
} | null;

interface ModelRequest extends Record<string, unknown> {
  model_id: string;
  reason: string | null;
  status: string;
  created_at: string;
}

type HomeData = {
  running_models: string[];
  public_api_url: string;
  sysmetrics: SysMetrics;
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

const requestColumns: TableColumn<ModelRequest>[] = [
  { key: "model_id", header: "Modèle" },
  { key: "reason", header: "Raison", renderCell: (row) => row.reason || "—" },
  {
    key: "status",
    header: "Statut",
    renderCell: (row) => <Badge label={STATUS_LABEL[row.status] || row.status} variant={STATUS_VARIANT[row.status] || "neutral"} />,
  },
  { key: "created_at", header: "Date", renderCell: (row) => row.created_at.slice(0, 16).replace("T", " ") },
];

export default function HomePage() {
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
                <Heading level={1}>Bonjour{firstName ? `, ${firstName}` : ""}</Heading>
                <Text type="supporting" color="secondary">
                  Ton accès self-service à l&apos;inférence LLM sur DGX Spark.
                </Text>
              </VStack>
              <Button
                label="Mes clés API"
                variant="primary"
                icon={<Icon icon={KeyIcon} size="sm" />}
                onClick={() => (window.location.href = "/keys")}
              />
            </HStack>

            <VStack gap={2}>
              <Text weight="semibold">Modèles disponibles maintenant</Text>
              {data && data.running_models.length === 0 && (
                <EmptyState
                  icon={<Icon icon={MoonIcon} size="lg" />}
                  title="Aucun modèle actif"
                  description="Demande le lancement d'un modèle."
                  actions={<Button label="Demander un modèle" variant="secondary" onClick={() => (window.location.href = "/request")} />}
                  isCompact
                />
              )}
              {data && data.running_models.length > 0 && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.running_models.map((m) => (
                    <Card key={m}>
                      <VStack gap={2}>
                        <Badge label="En ligne" variant="success" />
                        <Text weight="semibold" wordBreak="break-all">
                          {m}
                        </Text>
                        <Text type="supporting" color="secondary">
                          API : {data.public_api_url}
                        </Text>
                        <Button
                          label="Créer une clé API"
                          variant="secondary"
                          size="sm"
                          onClick={() => (window.location.href = "/keys")}
                        />
                      </VStack>
                    </Card>
                  ))}
                </Grid>
              )}
            </VStack>

            {data?.sysmetrics && (
              <VStack gap={2}>
                <Text weight="semibold">État du serveur</Text>
                <Card>
                  <VStack gap={4}>
                    <Grid columns={{ minWidth: 220, max: 3 }} gap={4}>
                      <VStack gap={1}>
                        <HStack hAlign="between">
                          <HStack gap={1} vAlign="center">
                            <Icon icon={CpuChipIcon} size="sm" />
                            <Text type="supporting" color="secondary">CPU</Text>
                          </HStack>
                          <Text hasTabularNumbers>{data.sysmetrics.cpu_pct} %</Text>
                        </HStack>
                        <ProgressBar label="CPU" isLabelHidden value={data.sysmetrics.cpu_pct} />
                      </VStack>
                      <VStack gap={1}>
                        <HStack hAlign="between">
                          <HStack gap={1} vAlign="center">
                            <Icon icon={CircleStackIcon} size="sm" />
                            <Text type="supporting" color="secondary">RAM</Text>
                          </HStack>
                          <Text hasTabularNumbers>
                            {data.sysmetrics.ram.used_gb} / {data.sysmetrics.ram.total_gb} Go
                          </Text>
                        </HStack>
                        <ProgressBar label="RAM" isLabelHidden value={data.sysmetrics.ram.pct} />
                      </VStack>
                      {data.sysmetrics.gpu && (
                        <VStack gap={1}>
                          <HStack hAlign="between">
                            <HStack gap={1} vAlign="center">
                              <Icon icon={BoltIcon} size="sm" />
                              <Text type="supporting" color="secondary">GPU</Text>
                            </HStack>
                            <Text hasTabularNumbers>
                              {Math.round(data.sysmetrics.gpu.util)} % · {Math.round(data.sysmetrics.gpu.power)} W ·{" "}
                              {Math.round(data.sysmetrics.gpu.temp)} °C
                            </Text>
                          </HStack>
                          <ProgressBar label="GPU" isLabelHidden value={data.sysmetrics.gpu.util} />
                        </VStack>
                      )}
                    </Grid>

                    {data.modelhealth && (
                      <HStack gap={5} wrap="wrap">
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">Modèle actif</Text>
                          <HStack gap={2} vAlign="center">
                            <Text weight="semibold">{data.modelhealth.model || "aucun"}</Text>
                            <Badge label={data.modelhealth.up ? "en ligne" : "arrêté"} variant={data.modelhealth.up ? "success" : "neutral"} />
                          </HStack>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">Débit</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.tps ?? "—"} tok/s</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">Sessions</Text>
                          <Text weight="semibold" hasTabularNumbers>
                            {data.modelhealth.running} / {data.modelhealth.max_seqs ?? "—"}
                          </Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">TTFT</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.ttft ?? "—"} s</Text>
                        </VStack>
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">Requêtes servies</Text>
                          <Text weight="semibold" hasTabularNumbers>{data.modelhealth.requests}</Text>
                        </VStack>
                      </HStack>
                    )}

                    {who?.is_admin && data.active_users && (
                      <VStack gap={2}>
                        <Text type="supporting" color="secondary">
                          Qui utilise le modèle · 2 dernières min · visible admin uniquement
                        </Text>
                        {data.active_users.length === 0 ? (
                          <Text type="supporting" color="secondary">Personne n&apos;utilise le modèle en ce moment.</Text>
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
                <Text weight="semibold">Mon utilisation — aujourd&apos;hui</Text>
                <Card>
                  <VStack gap={3}>
                    <Grid columns={3} gap={2}>
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">Tokens · 24 h</Text>
                        <Text size="xl" weight="bold" hasTabularNumbers>
                          {Math.round(data.usage.total).toLocaleString("fr-FR")}
                        </Text>
                      </VStack>
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">Clés actives</Text>
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
                <VStack gap={2}>
                  <HStack gap={2} vAlign="center">
                    <Icon icon={KeyIcon} size="sm" />
                    <Text weight="semibold">Mes clés API</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">
                    Crée des clés personnelles pour accéder aux modèles via l&apos;API OpenAI-compatible.
                  </Text>
                  <Text type="supporting" color="secondary">
                    Limite : {who?.is_admin ? "Illimitée (admin)" : `${data?.budget_tokens ?? "—"} tokens / ${data?.budget_duration ?? "—"}`}
                  </Text>
                  <Button label="Gérer mes clés" variant="secondary" onClick={() => (window.location.href = "/keys")} />
                </VStack>
              </Card>
              <Card>
                <VStack gap={2}>
                  <HStack gap={2} vAlign="center">
                    <Icon icon={MagnifyingGlassIcon} size="sm" />
                    <Text weight="semibold">Catalogue HuggingFace</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">
                    Parcours les modèles disponibles et demande le lancement de celui qui t&apos;intéresse.
                  </Text>
                  <Button label="Explorer les modèles" variant="secondary" onClick={() => (window.location.href = "/search")} />
                </VStack>
              </Card>
              <Card>
                <VStack gap={2}>
                  <HStack gap={2} vAlign="center">
                    <Icon icon={PaperAirplaneIcon} size="sm" />
                    <Text weight="semibold">Demander un modèle</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">
                    Tu connais un modèle que tu veux tester ? Envoie une demande à l&apos;admin.
                  </Text>
                  <Button label="Faire une demande" variant="secondary" onClick={() => (window.location.href = "/request")} />
                </VStack>
              </Card>
            </Grid>

            {data && data.my_requests.length > 0 && (
              <VStack gap={2}>
                <Text weight="semibold">Mes dernières demandes</Text>
                <Card padding={0}>
                  <Table<ModelRequest> data={data.my_requests} columns={requestColumns} idKey="model_id" density="balanced" dividers="rows" />
                </Card>
              </VStack>
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
