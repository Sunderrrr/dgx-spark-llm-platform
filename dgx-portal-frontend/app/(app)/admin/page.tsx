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
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
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
import { getJSON, postFormJSON, ForbiddenError } from "@/lib/api";
import { useT } from "@/lib/i18n";

type ModelCfg = { id: number; name: string; hf_model_id: string; engine: string; vllm_args: string };
type OcrCfg = { id: number; name: string; hf_model_id: string; vllm_args: string };
type VoiceCfg = { id: number; name: string; repo_id: string };
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
  voice_usage: UsageRow[];
  model_cfgs: ModelCfg[];
  v_status: VStatus;
  init_logs: string[];
  budget_reqs: BudgetRequest[];
  default_key_budget: number;
  default_key_duration: string;
  maintenance_mode: boolean;
  ocr_status: string;
  ocr_model_name: string | null;
  video_status: string;
  voice_status: string;
  voice_model_name: string | null;
  asr_status: string;
  ocr_cfgs: OcrCfg[];
  voice_cfgs: VoiceCfg[];
};

type CatalogKind = "llm" | "ocr" | "voice" | "video";

const MAX_LOG_LINES = 600;

const SIDECAR_VARIANT: Record<string, "success" | "warning" | "neutral" | "error"> = {
  running: "success",
  starting: "warning",
  stopped: "neutral",
};
const SIDECAR_LABEL: Record<string, string> = {
  running: "En ligne",
  starting: "Démarrage…",
  stopped: "Arrêté",
};

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
  const [newVoice, setNewVoice] = useState({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
  const [catalogKind, setCatalogKind] = useState<CatalogKind>("llm");
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
    // Certaines routes (démarrage des sidecars) renvoient { ok, error } avec un
    // statut non-2xx en cas de refus (mémoire insuffisante, etc.). On lit ce
    // résultat au lieu d'afficher « fait » systématiquement — le vrai bug qui
    // faisait croire qu'un lancement OCR/vidéo avait réussi alors qu'il OOMait.
    let errMsg: string | null = null;
    try {
      const res = await postFormJSON<{ ok?: boolean; error?: string }>(url, csrf, params);
      if (res && res.ok === false) errMsg = res.error || t("Échec de l'action.");
    } catch {
      // Réponse non-JSON (anciennes routes en redirect) → considéré comme OK.
    }
    showToast(errMsg ? { body: errMsg, type: "error" } : { body: t("Action effectuée."), type: "info" });
    refresh();
  }

  const st = data?.v_status.status;

  const budgetColumns: TableColumn<BudgetRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: t("Utilisateur"), renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "key_alias", header: t("Clé") },
    { key: "current_budget", header: t("Budget actuel"), renderCell: (r) => (r.current_budget ? Math.round(r.current_budget).toLocaleString("fr-FR") : "—") },
    { key: "reason", header: t("Raison"), renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    {
      key: "status",
      header: t("Statut"),
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
      header: t("Action"),
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
    { key: "username", header: t("Utilisateur") },
    { key: "tokens", header: t("Consommé aujourd'hui"), renderCell: (r) => Math.round(r.tokens || 0).toLocaleString("fr-FR") },
    { key: "max_budget", header: t("Budget / jour"), renderCell: (r) => (r.unlimited ? <Badge label="∞" variant="warning" /> : Math.round(r.max_budget || 0).toLocaleString("fr-FR")) },
    { key: "key_count", header: t("Clés") },
  ];

  const usageColumns = (countLabel: string): TableColumn<UsageRow & Record<string, unknown>>[] => [
    { key: "username", header: t("Utilisateur") },
    { key: "c", header: countLabel },
    { key: "last", header: t("Dernière utilisation"), renderCell: (r) => r.last.slice(0, 16).replace("T", " ") },
  ];

  const requestColumns: TableColumn<ModelRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: t("Utilisateur"), renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "model_id", header: t("Modèle") },
    { key: "reason", header: t("Raison"), renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    { key: "status", header: t("Statut"), renderCell: (r) => <Badge label={t(REQ_STATUS_LABEL[r.status] || r.status)} variant={REQ_STATUS_VARIANT[r.status] || "neutral"} /> },
    {
      key: "id" as keyof ModelRequest,
      header: t("Action"),
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
              {/* Une seule ligne pour les quatre backends : leur état et leur
                  démarrage/arrêt étaient auparavant éclatés entre une carte
                  vLLM isolée et une grille OCR/vidéo/voix. */}
              <Text weight="semibold">{t("Backends")}</Text>
              {data && (
                <Grid columns={{ minWidth: 200, max: 5 }} gap={3}>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={st === "running" ? "success" : st === "starting" ? "warning" : st === "error" ? "error" : "neutral"}
                            label={st === "running" ? t("En ligne") : st === "starting" ? t("Démarrage…") : st === "error" ? t("Erreur") : st === "unreachable" ? t("Runner inaccessible") : t("Arrêté")}
                          />
                          <Text weight="semibold">{t("LLM")}</Text>
                        </HStack>
                        {(st === "running" || st === "starting") && (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/model/stop")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">
                        {data.v_status.model || t("aucun modèle")}
                      </Text>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.ocr_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.ocr_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">OCR</Text>
                        </HStack>
                        {data.ocr_status === "running" || data.ocr_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/ocr/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/ocr/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">
                        {data.ocr_model_name || t("aucun modèle")}
                      </Text>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.video_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.video_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">{t("Vidéo")}</Text>
                        </HStack>
                        {data.video_status === "running" || data.video_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/video/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/video/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">MiniMax H3</Text>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.voice_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.voice_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">{t("Voix")}</Text>
                        </HStack>
                        {data.voice_status === "running" || data.voice_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/voice/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/voice/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">
                        {data.voice_model_name || t("aucun modèle")}
                      </Text>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.asr_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.asr_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">{t("Dictée")}</Text>
                        </HStack>
                        {data.asr_status === "running" || data.asr_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/asr/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/asr/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">whisper-large-v3-turbo</Text>
                    </VStack>
                  </Card>
                </Grid>
              )}

              {/* Catalogue unique : le type choisi pilote à la fois la liste
                  affichée et les champs du formulaire d'ajout, au lieu des
                  trois sections séparées (vLLM / OCR / voix) d'avant. */}
              <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
                <Text weight="semibold">{t("Catalogue")}</Text>
                <SegmentedControl label={t("Type de modèle")} value={catalogKind} onChange={(v) => setCatalogKind(v as CatalogKind)}>
                  <SegmentedControlItem value="llm" label={t("LLM")} />
                  <SegmentedControlItem value="ocr" label="OCR" />
                  <SegmentedControlItem value="voice" label={t("Voix")} />
                  <SegmentedControlItem value="video" label={t("Vidéo")} />
                </SegmentedControl>
              </HStack>

              {data && catalogKind === "llm" && (
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

              {data && catalogKind === "ocr" && (
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

              {data && catalogKind === "voice" && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.voice_cfgs.map((cfg) => (
                    <Card key={cfg.id}>
                      <VStack gap={2}>
                        <Text weight="semibold">{cfg.name}</Text>
                        <Text type="supporting" color="secondary" wordBreak="break-all">
                          {cfg.repo_id}
                        </Text>
                        <HStack gap={2}>
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/voice/catalog/launch", { voice_name: cfg.name })} />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => act(`/admin/voice/catalog/delete/${cfg.id}`)} />
                        </HStack>
                      </VStack>
                    </Card>
                  ))}
                  <Card>
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">{t("Ajouter un modèle voix")}</Text>
                      <TextInput label={t("Nom")} isLabelHidden value={newVoice.name} onChange={(v) => setNewVoice((s) => ({ ...s, name: v }))} placeholder={t("Nom (ex: qwen3-tts)")} size="sm" />
                      <Selector
                        label={t("Variante")}
                        isLabelHidden
                        value={newVoice.repo_id}
                        onChange={(v) => setNewVoice((s) => ({ ...s, repo_id: v ?? "Qwen3-TTS-12Hz-1.7B-Base" }))}
                        options={[
                          { value: "Qwen3-TTS-12Hz-1.7B-Base", label: "Qwen3-TTS 1.7B (10 langues)" },
                          { value: "Qwen3-TTS-12Hz-0.6B-Base", label: "Qwen3-TTS 0.6B (10 langues)" },
                          { value: "chatterbox-multilingual", label: "Chatterbox Multilingual (0.5B)" },
                          { value: "chatterbox-turbo", label: "Chatterbox Turbo (350M, EN)" },
                          { value: "chatterbox", label: "Chatterbox Original (0.5B, EN)" },
                        ]}
                        size="sm"
                      />
                      <Button
                        label={t("Ajouter")}
                        variant="secondary"
                        size="sm"
                        icon={<Icon icon={PlusIcon} size="sm" />}
                        onClick={async () => {
                          await act("/admin/voice/catalog/add", newVoice);
                          setNewVoice({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
                        }}
                      />
                    </VStack>
                  </Card>
                </Grid>
              )}

              {data && catalogKind === "video" && (
                <Card>
                  <VStack gap={1}>
                    <Text weight="semibold">MiniMax H3</Text>
                    <Text type="supporting" color="secondary">
                      {t("La vidéo n'a pas de catalogue : un seul workflow ComfyUI figé, démarré et arrêté depuis la ligne « Backends » ci-dessus.")}
                    </Text>
                  </VStack>
                </Card>
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
                {data && data.stats.budget_pending > 0 && <Badge label={`${data.stats.budget_pending} ${t("en attente")}`} variant="warning" />}
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
              <Text weight="semibold">{t("Utilisation voix par utilisateur")}</Text>
              <Card padding={0}>
                <Table<UsageRow & Record<string, unknown>>
                  data={data?.voice_usage ?? []}
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
