"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Center } from "@astryxdesign/core/Center";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Banner } from "@astryxdesign/core/Banner";
import { useToast } from "@astryxdesign/core/Toast";
import {
  PlayIcon,
  StopIcon,
  TrashIcon,
  CheckIcon,
  XMarkIcon,
  PlusIcon,
  MegaphoneIcon,
} from "@heroicons/react/24/outline";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { ShieldExclamationIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, postForm, ForbiddenError } from "@/lib/api";
import { useT } from "@/lib/i18n";

type ModelCfg = { id: number; name: string; hf_model_id: string; engine: string; vllm_args: string };
type OcrCfg = { id: number; name: string; hf_model_id: string; vllm_args: string };
type ModelRequest = { id: number; fullname: string; username: string; model_id: string; reason: string | null; status: string; created_at: string };
type BudgetRequest = {
  id: number;
  fullname: string;
  username: string;
  key_alias: string;
  current_budget: number | null;
  reason: string | null;
  status: string;
  created_at: string;
  granted_amount: number | null;
};
type SpendRow = { username: string; tokens: number; max_budget: number | null; unlimited: boolean; key_count: number };
type UsageRow = { username: string; c: number; last: string };
type VStatus = { status: string; model: string | null; pid: number | null; engine?: string };
type AdminData = {
  requests: ModelRequest[];
  running_models: string[];
  stats: { pending: number; done: number; rejected: number; budget_pending: number };
  spend_data: SpendRow[];
  ocr_usage: UsageRow[];
  video_usage: UsageRow[];
  model_cfgs: ModelCfg[];
  v_status: VStatus;
  init_logs: string[];
  budget_reqs: BudgetRequest[];
  default_key_budget: number;
  default_key_duration: string;
  maintenance_mode: boolean;
  ocr_status: string;
  video_status: string;
  ocr_cfgs: OcrCfg[];
};

const MAX_LOG_LINES = 600;

const REQ_STATUS_VARIANT: Record<string, "warning" | "success" | "error"> = { pending: "warning", done: "success", rejected: "error" };
const REQ_STATUS_LABEL: Record<string, string> = { pending: "En attente", done: "Lancé ✓", rejected: "Refusé" };

export default function AdminPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [data, setData] = useState<AdminData | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [newModel, setNewModel] = useState({ name: "", hf_model_id: "", engine: "vllm", vllm_args: "" });
  const [newOcr, setNewOcr] = useState({ name: "", hf_model_id: "", vllm_args: "" });
  const [announce, setAnnounce] = useState({ title: "", body: "" });
  const [settings, setSettings] = useState({ budget: "", duration: "" });

  function refresh() {
    getJSON<AdminData>("/api/admin")
      .then((d) => {
        setData(d);
        setLogs(d.init_logs);
        setSettings({ budget: String(d.default_key_budget), duration: d.default_key_duration });
      })
      .catch((e) => {
        if (e instanceof ForbiddenError) setForbidden(true);
      });
  }

  useEffect(refresh, []);
  useEffect(() => {
    if (forbidden) return;
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, [forbidden]);

  useEffect(() => {
    // Ne se rouvre pas une fois qu'on sait que l'accès est refusé — sinon
    // EventSource retente indéfiniment un flux réservé aux admins.
    if (forbidden) return;
    const es = new EventSource("/admin/runner/stream");
    es.onopen = () => setLogs([]);
    es.onmessage = (e) => setLogs((prev) => [...prev, e.data].slice(-MAX_LOG_LINES));
    es.addEventListener("clear", () => setLogs([]));
    return () => es.close();
  }, [forbidden]);

  async function act(url: string, params: Record<string, string> = {}) {
    if (!csrf) return;
    await postForm(url, csrf, params);
    showToast({ body: t("Action effectuée."), type: "info" });
    refresh();
  }

  const st = data?.v_status.status;

  const budgetColumns: TableColumn<BudgetRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: "Utilisateur", renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "key_alias", header: t("Clé") },
    { key: "current_budget", header: "Budget actuel", renderCell: (r) => (r.current_budget ? Math.round(r.current_budget).toLocaleString("fr-FR") : "—") },
    { key: "reason", header: "Raison", renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: "Date", renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    {
      key: "status",
      header: "Statut",
      renderCell: (r) =>
        r.status === "pending" ? (
          <Badge label={t("En attente")} variant="warning" />
        ) : r.status === "approved" ? (
          <Badge label={`+${Math.round(r.granted_amount || 0).toLocaleString("fr-FR")} ✓`} variant="success" />
        ) : (
          <Badge label={t("Refusé")} variant="error" />
        ),
    },
    {
      key: "id" as keyof BudgetRequest,
      header: "Action",
      renderCell: (r) =>
        r.status === "pending" ? (
          <HStack gap={1}>
            <BudgetApproveForm onApprove={(amount) => act(`/admin/budget/approve/${r.id}`, { amount })} />
            <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} onClick={() => act(`/admin/budget/reject/${r.id}`)} />
          </HStack>
        ) : null,
    },
  ];

  const consumptionColumns: TableColumn<SpendRow & Record<string, unknown>>[] = [
    { key: "username", header: "Utilisateur" },
    { key: "tokens", header: t("Consommé aujourd'hui"), renderCell: (r) => Math.round(r.tokens || 0).toLocaleString("fr-FR") },
    { key: "max_budget", header: "Budget / jour", renderCell: (r) => (r.unlimited ? <Badge label="∞" variant="warning" /> : Math.round(r.max_budget || 0).toLocaleString("fr-FR")) },
    { key: "key_count", header: "Clés" },
  ];

  const usageColumns = (countLabel: string): TableColumn<UsageRow & Record<string, unknown>>[] => [
    { key: "username", header: "Utilisateur" },
    { key: "c", header: countLabel },
    { key: "last", header: t("Dernière utilisation"), renderCell: (r) => r.last.slice(0, 16).replace("T", " ") },
  ];

  const requestColumns: TableColumn<ModelRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: "Utilisateur", renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "model_id", header: "Modèle" },
    { key: "reason", header: "Raison", renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: "Date", renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    { key: "status", header: "Statut", renderCell: (r) => <Badge label={t(REQ_STATUS_LABEL[r.status] || r.status)} variant={REQ_STATUS_VARIANT[r.status] || "neutral"} /> },
    {
      key: "id" as keyof ModelRequest,
      header: "Action",
      renderCell: (r) => (
        <HStack gap={1}>
          {r.status !== "done" && <Button label={t("Lancé")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={CheckIcon} size="sm" />} onClick={() => act(`/admin/update/${r.id}`, { status: "done" })} />}
          {r.status !== "rejected" && <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} onClick={() => act(`/admin/update/${r.id}`, { status: "rejected" })} />}
        </HStack>
      ),
    },
  ];

  if (forbidden) {
    return (
      <Layout
        height="fill"
        content={
          <LayoutContent padding={6} isScrollable>
            <Center axis="both" height="100%">
              <EmptyState
                icon={<Icon icon={ShieldExclamationIcon} size="lg" color="secondary" />}
                title={t("Accès réservé aux administrateurs")}
                description={t("Ton compte n'a pas les droits nécessaires pour voir cette page.")}
                actions={<Button label={t("Retour à l'accueil")} variant="primary" href="/" />}
              />
            </Center>
          </LayoutContent>
        }
      />
    );
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={6}>
            <VStack gap={1}>
              <Heading level={1}>{t("Administration")}</Heading>
              <Text type="supporting" color="secondary">{t("Pilotage des modèles, quotas de tokens et demandes des utilisateurs.")}</Text>
            </VStack>

            {data && (
              <Banner
                status={data.maintenance_mode ? "warning" : "info"}
                title={data.maintenance_mode ? t("Mode maintenance actif") : t("Mode maintenance")}
                description={t(
                  "Bloque l'accès à l'API et au chat/OCR/vidéo pour les non-admins, sans arrêter les modèles. Les admins gardent l'accès.",
                )}
                endContent={
                  <Button
                    label={data.maintenance_mode ? t("Désactiver") : t("Activer")}
                    variant={data.maintenance_mode ? "secondary" : "primary"}
                    size="sm"
                    onClick={() => act("/admin/maintenance/toggle")}
                  />
                }
              />
            )}

            <VStack gap={3}>
              <Text weight="semibold">{t("Modèles vLLM")}</Text>
              {data && (
                <Card>
                  <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                    <HStack gap={2} vAlign="center">
                      <StatusDot
                        variant={st === "running" ? "success" : st === "starting" ? "warning" : st === "error" ? "error" : "neutral"}
                        label={st === "running" ? t("En ligne") : st === "starting" ? t("Démarrage…") : st === "error" ? t("Erreur") : st === "unreachable" ? t("Runner inaccessible") : t("Arrêté")}
                      />
                      {data.v_status.model && <Text weight="semibold">{data.v_status.model}</Text>}
                    </HStack>
                    {(st === "running" || st === "starting") && (
                      <Button label={t("Arrêter")} variant="secondary" size="sm" icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/model/stop")} />
                    )}
                  </HStack>
                </Card>
              )}

              {data && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.model_cfgs.map((cfg) => (
                    <Card key={cfg.id}>
                      <VStack gap={2}>
                        <HStack gap={2} vAlign="center">
                          <Text weight="semibold">{cfg.name}</Text>
                          <Badge label={cfg.engine} variant="neutral" />
                        </HStack>
                        <Text type="supporting" color="secondary" wordBreak="break-all">
                          {cfg.hf_model_id}
                        </Text>
                        <HStack gap={2}>
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/model/launch", { model_name: cfg.name })} />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => act(`/admin/model/delete/${cfg.id}`)} />
                        </HStack>
                      </VStack>
                    </Card>
                  ))}
                  <Card>
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">{t("Ajouter un modèle")}</Text>
                      <TextInput label={t("Nom")} isLabelHidden value={newModel.name} onChange={(v) => setNewModel((s) => ({ ...s, name: v }))} placeholder={t("Nom (ex: llama-3-8b)")} size="sm" />
                      <TextInput label={t("HF ID")} isLabelHidden value={newModel.hf_model_id} onChange={(v) => setNewModel((s) => ({ ...s, hf_model_id: v }))} placeholder={t("HF ID")} size="sm" />
                      <Selector
                        label={t("Moteur")}
                        isLabelHidden
                        value={newModel.engine}
                        onChange={(v) => setNewModel((s) => ({ ...s, engine: v ?? "vllm" }))}
                        options={[
                          { value: "vllm", label: "vLLM (safetensors)" },
                          { value: "llamacpp", label: "llama.cpp (GGUF)" },
                          { value: "ds4", label: "ds4 (GGUF NVFP4)" },
                        ]}
                      />
                      <TextInput label={t("Args")} isLabelHidden value={newModel.vllm_args} onChange={(v) => setNewModel((s) => ({ ...s, vllm_args: v }))} placeholder={t("Args du moteur")} size="sm" />
                      <Button
                        label={t("Ajouter")}
                        variant="secondary"
                        size="sm"
                        icon={<Icon icon={PlusIcon} size="sm" />}
                        onClick={async () => {
                          await act("/admin/model/add", newModel);
                          setNewModel({ name: "", hf_model_id: "", engine: "vllm", vllm_args: "" });
                        }}
                      />
                    </VStack>
                  </Card>
                </Grid>
              )}

              <Text weight="semibold">{t("OCR & Vidéo")}</Text>
              {data && (
                <Grid columns={{ minWidth: 240, max: 2 }} gap={3}>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center">
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={data.ocr_status === "running" ? "success" : data.ocr_status === "stopped" ? "neutral" : "error"}
                            label={data.ocr_status === "running" ? t("En ligne") : data.ocr_status === "stopped" ? t("Arrêté") : t("Injoignable")}
                          />
                          <Text weight="semibold">OCR — Unlimited-OCR</Text>
                        </HStack>
                        {data.ocr_status === "running" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/ocr/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/ocr/start")} />
                        )}
                      </HStack>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center">
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={data.video_status === "running" ? "success" : data.video_status === "stopped" ? "neutral" : "error"}
                            label={data.video_status === "running" ? t("En ligne") : data.video_status === "stopped" ? t("Arrêté") : t("Injoignable")}
                          />
                          <Text weight="semibold">Vidéo — MiniMax H3</Text>
                        </HStack>
                        {data.video_status === "running" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/video/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/video/start")} />
                        )}
                      </HStack>
                    </VStack>
                  </Card>
                </Grid>
              )}

              <Text type="supporting" color="secondary">{t("Catalogue OCR")}</Text>
              {data && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.ocr_cfgs.map((cfg) => (
                    <Card key={cfg.id}>
                      <VStack gap={2}>
                        <Text weight="semibold">{cfg.name}</Text>
                        <Text type="supporting" color="secondary" wordBreak="break-all">
                          {cfg.hf_model_id}
                        </Text>
                        <HStack gap={2}>
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/ocr/catalog/launch", { ocr_name: cfg.name })} />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => act(`/admin/ocr/catalog/delete/${cfg.id}`)} />
                        </HStack>
                      </VStack>
                    </Card>
                  ))}
                  <Card>
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">{t("Ajouter un modèle OCR")}</Text>
                      <TextInput label={t("Nom")} isLabelHidden value={newOcr.name} onChange={(v) => setNewOcr((s) => ({ ...s, name: v }))} placeholder={t("Nom (ex: unlimited-ocr)")} size="sm" />
                      <TextInput label={t("HF ID")} isLabelHidden value={newOcr.hf_model_id} onChange={(v) => setNewOcr((s) => ({ ...s, hf_model_id: v }))} placeholder={t("HF ID")} size="sm" />
                      <TextInput label={t("Args")} isLabelHidden value={newOcr.vllm_args} onChange={(v) => setNewOcr((s) => ({ ...s, vllm_args: v }))} placeholder={t("Args du moteur")} size="sm" />
                      <Button
                        label={t("Ajouter")}
                        variant="secondary"
                        size="sm"
                        icon={<Icon icon={PlusIcon} size="sm" />}
                        onClick={async () => {
                          await act("/admin/ocr/catalog/add", newOcr);
                          setNewOcr({ name: "", hf_model_id: "", vllm_args: "" });
                        }}
                      />
                    </VStack>
                  </Card>
                </Grid>
              )}

              <Card>
                <VStack gap={2}>
                  <Text type="supporting" color="secondary">{t("Publier une annonce")}</Text>
                  <HStack gap={2} wrap="wrap">
                    <TextInput label={t("Titre")} isLabelHidden value={announce.title} onChange={(v) => setAnnounce((s) => ({ ...s, title: v }))} placeholder={t("Titre")} size="sm" />
                    <TextInput label={t("Détails")} isLabelHidden value={announce.body} onChange={(v) => setAnnounce((s) => ({ ...s, body: v }))} placeholder={t("Détails (optionnel)")} size="sm" />
                    <Button
                      label={t("Publier")}
                      variant="secondary"
                      size="sm"
                      icon={<Icon icon={MegaphoneIcon} size="sm" />}
                      onClick={async () => {
                        await act("/admin/announce", announce);
                        setAnnounce({ title: "", body: "" });
                      }}
                    />
                  </HStack>
                </VStack>
              </Card>

              <Card>
                <VStack gap={2}>
                  <Text weight="semibold">Logs — {data?.v_status.model || t("aucun modèle")}</Text>
                  <CodeBlock code={logs.join("\n")} language="plaintext" hasCopyButton width="100%" maxHeight={280} />
                </VStack>
              </Card>
            </VStack>

            {data && (
              <Grid columns={3} gap={3}>
                <Card>
                  <VStack gap={0} align="center">
                    <Text size="2xl" weight="bold">{data.stats.pending}</Text>
                    <Text type="supporting" color="secondary">{t("Demandes en attente")}</Text>
                  </VStack>
                </Card>
                <Card>
                  <VStack gap={0} align="center">
                    <Text size="2xl" weight="bold" color="accent">{data.stats.done}</Text>
                    <Text type="supporting" color="secondary">{t("Lancées")}</Text>
                  </VStack>
                </Card>
                <Card>
                  <VStack gap={0} align="center">
                    <Text size="2xl" weight="bold">{data.stats.rejected}</Text>
                    <Text type="supporting" color="secondary">{t("Refusées")}</Text>
                  </VStack>
                </Card>
              </Grid>
            )}

            <VStack gap={2}>
              <Text weight="semibold">{t("Limite de tokens par défaut (nouvelles clés)")}</Text>
              <Card>
                <HStack gap={2} vAlign="end" wrap="wrap">
                  <TextInput label={t("Tokens générés")} value={settings.budget} onChange={(v) => setSettings((s) => ({ ...s, budget: v }))} size="sm" />
                  <TextInput label={t("Durée (ex: 1d, 7d, 12h)")} value={settings.duration} onChange={(v) => setSettings((s) => ({ ...s, duration: v }))} size="sm" />
                  <Button
                    label={t("Appliquer")}
                    variant="secondary"
                    size="sm"
                    icon={<Icon icon={CheckIcon} size="sm" />}
                    onClick={() => act("/admin/settings", { default_key_budget: settings.budget, default_key_duration: settings.duration })}
                  />
                </HStack>
              </Card>
            </VStack>

            <VStack gap={2}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{t("Demandes de tokens")}</Text>
                {data && data.stats.budget_pending > 0 && <Badge label={`${data.stats.budget_pending} en attente`} variant="warning" />}
              </HStack>
              <Card padding={0}>
                <Table<BudgetRequest & Record<string, unknown>> data={data?.budget_reqs ?? []} columns={budgetColumns} idKey="id" density="balanced" dividers="rows" />
              </Card>
            </VStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Consommation par utilisateur")}</Text>
              <Card padding={0}>
                <Table<SpendRow & Record<string, unknown>> data={data?.spend_data ?? []} columns={consumptionColumns} idKey="username" density="balanced" dividers="rows" />
              </Card>
            </VStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Utilisation OCR par utilisateur")}</Text>
              <Text type="supporting" color="secondary">
                {t("Ne passe pas par une clé API — jamais visible dans la conso LiteLLM ci-dessus.")}
              </Text>
              <Card padding={0}>
                <Table<UsageRow & Record<string, unknown>>
                  data={data?.ocr_usage ?? []}
                  columns={usageColumns(t("Extractions"))}
                  idKey="username"
                  density="balanced"
                  dividers="rows"
                />
              </Card>
            </VStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Utilisation vidéo par utilisateur")}</Text>
              <Card padding={0}>
                <Table<UsageRow & Record<string, unknown>>
                  data={data?.video_usage ?? []}
                  columns={usageColumns(t("Générations"))}
                  idKey="username"
                  density="balanced"
                  dividers="rows"
                />
              </Card>
            </VStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Demandes de modèles")}</Text>
              <Card padding={0}>
                <Table<ModelRequest & Record<string, unknown>> data={data?.requests ?? []} columns={requestColumns} idKey="id" density="balanced" dividers="rows" />
              </Card>
            </VStack>
          </VStack>
        </LayoutContent>
      }
    />
  );
}

function BudgetApproveForm({ onApprove }: { onApprove: (amount: string) => void }) {
  const t = useT();
  const [amount, setAmount] = useState("");
  return (
    <HStack gap={1}>
      <TextInput label="Tokens" isLabelHidden value={amount} onChange={setAmount} placeholder={t("tokens")} size="sm" />
      <Button
        label={t("Approuver")}
        variant="ghost"
        size="sm"
        isIconOnly
        icon={<Icon icon={CheckIcon} size="sm" />}
        isDisabled={!amount}
        onClick={() => onApprove(amount)}
      />
    </HStack>
  );
}
