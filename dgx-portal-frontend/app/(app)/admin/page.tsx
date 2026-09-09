"use client";

import { useCallback, useEffect, useState } from "react";
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
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import {
  PlayIcon,
  StopIcon,
  TrashIcon,
  CheckIcon,
  XMarkIcon,
  PlusIcon,
  MegaphoneIcon,
  ArrowDownIcon,
} from "@heroicons/react/24/outline";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { ShieldExclamationIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, postForm, postFormJSON, ForbiddenError } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { UserLookup } from "./_components/UserLookup";
import { EmailConfig } from "./_components/EmailConfig";

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
type BudgetGrant = { username: string; base_budget: number; current_budget: number; expires_at: string };
type SpendRow = { username: string; tokens: number; max_budget: number | null; unlimited: boolean; key_count: number };
type AuditRow = { id: number; username: string; action: string; detail: string; created_at: string };
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
  budget_grants: BudgetGrant[];
  default_key_budget: number;
  default_key_duration: string;
  maintenance_mode: boolean;
  ocr_status: string;
  ocr_model_name: string | null;
  video_status: string;
  voice_status: string;
  voice_model_name: string | null;
  asr_status: string;
  image_status: string;
  image_model_name: string | null;
  image_model_ids: string[];
  music_status: string;
  music_model_name: string | null;
  ocr_cfgs: OcrCfg[];
  voice_cfgs: VoiceCfg[];
};

type CatalogKind = "llm" | "ocr" | "voice" | "video" | "image" | "music";

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
  const [audit, setAudit] = useState<AuditRow[]>([]);
  // Which model's logs the admin is viewing. "llm" is the live SSE stream
  // (the chat model); the sidecars are fetched on demand + polled.
  const [logKind, setLogKind] = useState<"llm" | "ocr" | "voice" | "image" | "music" | "video">("llm");
  const [sidecarLogs, setSidecarLogs] = useState<string[]>([]);
  const [newModel, setNewModel] = useState({ name: "", hf_model_id: "", engine: "vllm", vllm_args: "" });
  const [newOcr, setNewOcr] = useState({ name: "", hf_model_id: "", vllm_args: "" });
  const [newVoice, setNewVoice] = useState({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
  const [catalogKind, setCatalogKind] = useState<CatalogKind>("llm");
  const [announce, setAnnounce] = useState({ title: "", body: "" });
  const [settings, setSettings] = useState({ budget: "", duration: "" });
  const [musicModel, setMusicModel] = useState("MiniMaxAI/MiniMax-Music3");

  // Texte affiché dans le visualiseur de logs, et suivi automatique du bas —
  // même comportement que le panneau du Playground : on colle au bas tant que
  // l'admin n'a pas remonté ; s'il remonte, on arrête de le ramener en bas et
  // une flèche apparaît pour redescendre et réactiver le suivi. `active` est
  // toujours vrai ici (contrairement au Playground où il ne suit que pendant
  // le flux) : les logs continuent d'arriver tant que la page est ouverte.
  const logText = (logKind === "llm" ? logs : sidecarLogs).join("\n");
  const {
    setRef: attachLogsScroller,
    showButton: showLogsJump,
    scrollToBottom: logsJumpDown,
  } = useStickToBottom(logText, true);

  // CodeBlock gère lui-même son défilement (dès qu'on lui donne un maxHeight) et
  // n'expose pas ce conteneur. On pose donc la ref sur un parent et on descend
  // chercher l'élément réellement défilable — vérifié au navigateur : sans ça, le
  // texte est rogné par un enfant en overflow:hidden et plus rien ne défile.
  const setLogsScrollRef = useCallback(
    (node: HTMLElement | null) => {
      if (!node) return attachLogsScroller(null);
      const scroller =
        Array.from(node.querySelectorAll<HTMLElement>("*")).find((e) => {
          const o = getComputedStyle(e).overflowY;
          return o === "auto" || o === "scroll";
        }) ?? node;
      attachLogsScroller(scroller);
    },
    [attachLogsScroller],
  );

  // `amorce` : ne recopier init_logs QUE lors du premier chargement. Ce
  // rafraîchissement tourne toutes les 8 s, et il écrasait à chaque passage les
  // lignes accumulées par le flux SSE avec un instantané figé — donc en régime
  // normal le panneau reperdait tout le direct trois fois par tour de flux, et
  // si l'instantané était vide (cf. runner_logs côté portail) il se vidait
  // purement et simplement. Le flux est la source de vérité une fois ouvert ;
  // init_logs ne sert qu'à remplir le panneau avant sa première ligne.
  function refresh(amorce = false) {
    getJSON<AdminData>("/api/admin")
      .then((d) => {
        setData(d);
        if (amorce) setLogs(d.init_logs);
        setSettings({ budget: String(d.default_key_budget), duration: d.default_key_duration });
      })
      .catch((e) => {
        if (e instanceof ForbiddenError) setForbidden(true);
      });
  }

  useEffect(() => { refresh(true); }, []);
  // Journal d'audit : chargé une fois à l'ouverture (les actions sensibles sont
  // rares, pas besoin de polling rapproché).
  useEffect(() => {
    getJSON<AuditRow[]>("/admin/audit")
      .then((d) => setAudit(d ?? []))
      .catch(() => {});
  }, []);
  // Re-poll admin data every 8s; stops once access is known forbidden to
  // avoid hammering a 403.
  useEffect(() => {
    if (forbidden) return;
    const id = setInterval(() => refresh(), 8000);
    return () => clearInterval(id);
  }, [forbidden]);

  useEffect(() => {
    // Doesn't reopen once we know access is forbidden — otherwise
    // EventSource would retry indefinitely an admin-only stream.
    if (forbidden) return;
    const es = new EventSource("/admin/runner/stream");
    // Le flux rejoue TOUT son tampon a chaque connexion : on vide donc a
    // l'ouverture, sinon ses lignes s'ajouteraient a celles de l'amorce et le
    // panneau afficherait tout en double.
    es.onopen = () => setLogs([]);

    es.onmessage = (e) => setLogs((prev) => [...prev, e.data].slice(-MAX_LOG_LINES));
    es.addEventListener("clear", () => setLogs([]));
    return () => es.close();
  }, [forbidden]);

  // Sidecar log tabs: fetch the selected sidecar's tail on switch, then poll
  // every 5s. "llm" uses the live SSE stream above instead, so nothing to fetch.
  useEffect(() => {
    if (forbidden || logKind === "llm") return;
    let alive = true;
    const load = () => {
      getJSON<{ logs?: string[] }>(`/admin/sidecar-logs/${logKind}`)
        .then((d) => { if (alive) setSidecarLogs(d?.logs ?? []); })
        .catch(() => { if (alive) setSidecarLogs([]); });
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, [logKind, forbidden]);

  async function act(url: string, params: Record<string, string> = {}) {
    if (!csrf) return;
    // Some routes (sidecar startup) return { ok, error } with a non-2xx
    // status on refusal (insufficient memory, etc.). We read that result
    // instead of always showing "done" — the real bug that made an OCR/video
    // launch look successful while it was actually OOMing.
    let errMsg: string | null = null;
    try {
      const res = await postFormJSON<{ ok?: boolean; error?: string }>(url, csrf, params);
      if (res && res.ok === false) errMsg = res.error ? t(res.error) : t("Échec de l'action.");
    } catch {
      // Non-JSON response (older redirect routes) → treated as OK.
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
            <BudgetApproveForm fullname={r.fullname} currentBudget={r.current_budget} onApprove={(amount, days) => act(`/admin/budget/approve/${r.id}`, { amount, grant_days: days })} />
            <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} onClick={() => act(`/admin/budget/reject/${r.id}`)} />
          </HStack>
        ) : null,
    },
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

  const auditColumns: TableColumn<AuditRow>[] = [
    { key: "username", header: t("Acteur") },
    { key: "action", header: t("Action"), renderCell: (r) => <Badge label={r.action} variant="neutral" /> },
    { key: "detail", header: t("Détail") },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
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

            <EmailConfig />

            <VStack gap={3}>
              {/* A single row for the four backends: their status and
                  start/stop were previously split between an isolated vLLM
                  card and an OCR/video/voice grid. */}
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
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.image_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.image_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">{t("Image")}</Text>
                        </HStack>
                        {data.image_status === "running" || data.image_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/image/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/image/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">
                        {data.image_model_name || t("aucun modèle")}
                      </Text>
                    </VStack>
                  </Card>
                  <Card>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="center" gap={2}>
                        <HStack gap={2} vAlign="center">
                          <StatusDot
                            variant={SIDECAR_VARIANT[data.music_status] ?? "error"}
                            label={t(SIDECAR_LABEL[data.music_status] ?? "Injoignable")}
                          />
                          <Text weight="semibold">{t("Musique")}</Text>
                        </HStack>
                        {data.music_status === "running" || data.music_status === "starting" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} onClick={() => act("/admin/music/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/music/start")} />
                        )}
                      </HStack>
                      <Text type="supporting" color="secondary" wordBreak="break-all">
                        {data.music_model_name || t("aucun modèle")}
                      </Text>
                    </VStack>
                  </Card>
                </Grid>
              )}

              {/* Single catalog: the chosen type drives both the displayed
                  list and the add-form fields, instead of the three separate
                  sections (vLLM / OCR / voice) from before. */}
              <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
                <Text weight="semibold">{t("Catalogue")}</Text>
                <SegmentedControl label={t("Type de modèle")} value={catalogKind} onChange={(v) => setCatalogKind(v as CatalogKind)}>
                  <SegmentedControlItem value="llm" label={t("LLM")} />
                  <SegmentedControlItem value="ocr" label="OCR" />
                  <SegmentedControlItem value="voice" label={t("Voix")} />
                  <SegmentedControlItem value="image" label={t("Image")} />
                  <SegmentedControlItem value="music" label={t("Musique")} />
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

              {data && catalogKind === "image" && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  {data.image_model_ids.map((mid) => (
                    <Card key={mid}>
                      <VStack gap={2}>
                        <Text weight="semibold" wordBreak="break-all">{mid}</Text>
                        <Text type="supporting" color="secondary">
                          {mid === data.image_model_name ? t("Modèle actif") : t("diffusers · text-to-image")}
                        </Text>
                        <HStack gap={2}>
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} onClick={() => act("/admin/image/launch", { model_id: mid })} />
                        </HStack>
                      </VStack>
                    </Card>
                  ))}
                  <Card>
                    <VStack gap={1}>
                      <Text type="supporting" color="secondary">{t("Ajouter un modèle image")}</Text>
                      <Text type="supporting" color="secondary">
                        {t("Les poids image (gated, ~35 Go) se téléchargent côté hôte puis s'ajoutent à la liste blanche — même principe que l'OCR/voix. Lance ensuite le modèle ci-contre.")}
                      </Text>
                    </VStack>
                  </Card>
                </Grid>
              )}

              {data && catalogKind === "music" && (
                <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
                  <Card>
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">{t("Lancer un modèle musique")}</Text>
                      <TextInput
                        label={t("Modèle musique")}
                        isLabelHidden
                        value={musicModel}
                        onChange={setMusicModel}
                        placeholder={t("Identifiant HuggingFace (ex : MiniMaxAI/MiniMax-Music3)")}
                        size="sm"
                      />
                      <Text type="supporting" color="secondary">
                        {t("Le conteneur télécharge le modèle depuis HuggingFace au démarrage — le premier lancement peut prendre plusieurs minutes.")}
                      </Text>
                      <Button
                        label={t("Lancer")}
                        variant="primary"
                        size="sm"
                        icon={<Icon icon={PlayIcon} size="sm" />}
                        onClick={() => act("/admin/music/launch", { model_id: musicModel.trim() })}
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
                  <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                    <Text weight="semibold">
                      {logKind === "llm"
                        ? `Logs — ${data?.v_status.model || t("aucun modèle")}`
                        : `Logs — ${logKind.toUpperCase()}`}
                    </Text>
                    <SegmentedControl label={t("Logs à afficher")} value={logKind} onChange={(v) => { setSidecarLogs([]); setLogKind(v as typeof logKind); }}>
                      <SegmentedControlItem value="llm" label={t("LLM")} />
                      <SegmentedControlItem value="ocr" label="OCR" />
                      <SegmentedControlItem value="voice" label={t("Voix")} />
                      <SegmentedControlItem value="image" label={t("Image")} />
                      <SegmentedControlItem value="music" label={t("Musique")} />
                      <SegmentedControlItem value="video" label={t("Vidéo")} />
                    </SegmentedControl>
                  </HStack>
                  {/* Le maxHeight rend le défilement à CodeBlock : contraindre un
                      parent à la place ne marche pas, CodeBlock se fait comprimer et
                      rogne son texte (overflow:hidden interne), si bien que plus rien
                      ne déborde ni ne défile. La ref sert seulement de point d'entrée
                      pour retrouver son scroller. Le conteneur est en position
                      relative pour ancrer la flèche À L'INTÉRIEUR du cadre. */}
                  <VStack ref={setLogsScrollRef} style={{ position: "relative" }}>
                    <CodeBlock
                      code={logText || t("Aucun log — ce modèle n'est pas démarré.")}
                      language="plaintext"
                      hasCopyButton
                      width="100%"
                      maxHeight={280}
                    />
                    {/* Flèche seule, centrée en bas du cadre (le coin haut-droit
                        est déjà pris par le bouton Copier de CodeBlock). */}
                    {showLogsJump && (
                      <HStack
                        style={{
                          position: "absolute",
                          bottom: "var(--spacing-3)",
                          left: "50%",
                          transform: "translateX(-50%)",
                          zIndex: 2,
                        }}
                      >
                        <Button
                          label={t("Descendre")}
                          variant="primary"
                          size="sm"
                          isIconOnly
                          icon={<Icon icon={ArrowDownIcon} size="sm" />}
                          onClick={logsJumpDown}
                        />
                      </HStack>
                    )}
                  </VStack>
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
              {(data?.budget_grants?.length ?? 0) > 0 && (
                <VStack gap={1}>
                  {data!.budget_grants.map((g) => (
                    <Text key={g.username} type="supporting" color="secondary">
                      {t("Boost temporaire — {user} : {total} tokens jusqu'au {date} UTC (retour à {base}).")
                        .replace("{user}", g.username)
                        .replace("{total}", Math.round(g.current_budget).toLocaleString("fr-FR"))
                        .replace("{date}", g.expires_at.slice(0, 16).replace("T", " "))
                        .replace("{base}", Math.round(g.base_budget).toLocaleString("fr-FR"))}
                    </Text>
                  ))}
                </VStack>
              )}
              <BudgetSetForm rows={data?.spend_data ?? []} />
            </VStack>

            <UserLookup
              spend={data?.spend_data ?? []}
              ocr={data?.ocr_usage ?? []}
              video={data?.video_usage ?? []}
              voice={data?.voice_usage ?? []}
              requests={data?.requests ?? []}
            />

            <VStack gap={2}>
              <Text weight="semibold">{t("Demandes de modèles")}</Text>
              <Card padding={0}>
                <Table<ModelRequest & Record<string, unknown>> data={data?.requests ?? []} columns={requestColumns} idKey="id" density="balanced" dividers="rows" />
              </Card>
            </VStack>

            <VStack gap={2}>
              <Text weight="semibold">{t("Journal d'audit")}</Text>
              <Card padding={0}>
                <Table<AuditRow> data={audit} columns={auditColumns} idKey="id" density="balanced" dividers="rows" emptyState={<EmptyState icon={<Icon icon={ShieldExclamationIcon} size="lg" color="secondary" />} title={t("Aucune entrée")} description={t("Les actions sensibles apparaîtront ici.")} />} />
              </Card>
            </VStack>
          </VStack>
        </LayoutContent>
      }
    />
  );
}

const BUDGET_PRESETS = [10000000, 50000000, 100000000];

/** « 10M » plutôt que « 10 000 000 » sur les boutons : lisible d'un coup
    d'œil. Les montants qui ne tombent pas juste restent en clair. */
const fmtCompact = (n: number) =>
  n >= 1_000_000 && n % 1_000_000 === 0 ? `${Math.round(n / 1_000_000)}M` : Math.round(n).toLocaleString("fr-FR");

function BudgetSetForm({ rows }: { rows: SpendRow[] }) {
  /** Redéfinir le plafond d'un compte : montant EXACT (pas un ajout) — sert
     à BAISSER un quota (200M → 50M) autant qu'à le hausser. L'utilisateur se
     CHOISIT dans une liste (avec son plafond actuel affiché) : pas de nom à
     taper au hasard. */
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [user, setUser] = useState("");
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);
  const fmt = (n: number) => Math.round(n).toLocaleString("fr-FR");
  const sel = rows.find((r) => r.username === user);
  return (
    <Card>
      <VStack gap={2}>
        <Text weight="semibold">{t("Redéfinir le plafond d'un compte")}</Text>
        <Text type="supporting" color="secondary">
          {t("Montant exact (pas un ajout) — sert aussi à baisser un quota. La fenêtre de reset reste celle du portail.")}
        </Text>
        <Selector
          label={t("Utilisateur")}
          hasSearch
          searchPlaceholder={t("Rechercher...")}
          placeholder={t("Choisir un compte")}
          options={rows.map((r) => ({
            value: r.username,
            label: `${r.username} · ${r.unlimited ? t("illimité") : fmtCompact(r.max_budget || 0)}`,
          }))}
          value={user}
          onChange={setUser}
        />
        {sel && (
          <Text type="supporting" color="secondary">
            {t("Plafond actuel :")} {sel ? (sel.unlimited ? t("Illimitée (admin)") : fmtCompact(sel.max_budget || 0)) : ""}
          </Text>
        )}
        <HStack gap={1}>
          {[50000000, 100000000, 200000000].map((v) => (
            <Button key={v} label={fmtCompact(v)} variant="secondary" size="sm" onClick={() => setBudget(String(v))} />
          ))}
        </HStack>
        <HStack gap={2} vAlign="end">
          <TextInput label={t("Nouveau plafond (tokens)")} value={budget} onChange={setBudget} placeholder="50000000" size="sm" />
          <Button
            label={t("Redéfinir")}
            variant="primary"
            size="sm"
            isDisabled={!user || !budget || busy}
            onClick={async () => {
              setBusy(true);
              try {
                await postForm(`/admin/users/${encodeURIComponent(user.trim())}/budget/set`, csrf, { budget: budget.trim() });
                showToast({ body: t("Budget redéfini.") });
                setBudget("");
              } finally {
                setBusy(false);
              }
            }}
          />
        </HStack>
      </VStack>
    </Card>
  );
}

function BudgetApproveForm({ onApprove, fullname, currentBudget }: {
  onApprove: (amount: string, days: string) => void;
  fullname: string;
  currentBudget: number | null;
}) {
  /** UN bouton par demande : « Approuver » ouvre un dialog avec montants en
     un clic (+10M/+50M/+100M), une durée en segments (Permanente/1j/3j/7j/30j)
     et l'aperçu du résultat AVANT de confirmer. Plus aucun champ à deviner. */
  const t = useT();
  const [isOpen, setIsOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [days, setDays] = useState("permanent");
  const fmt = (n: number) => Math.round(n).toLocaleString("fr-FR");
  const base = currentBudget || 0;
  const total = base + (parseFloat(amount) || 0);
  // eslint-disable-next-line react-hooks/purity -- aperçu « retour à la base le … » : la date est par nature relative à maintenant, recalculée à chaque ouverture du dialog
  const expire = days === "permanent" ? null : new Date(Date.now() + parseInt(days, 10) * 86400000);
  return (
    <>
      <Button label={t("Approuver")} variant="ghost" size="sm" onClick={() => setIsOpen(true)} />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Accorder des tokens")} subtitle={`${fullname} — ${t("Actuel :")} ${fmt(base)}`} />
        <VStack gap={3}>
          <VStack gap={1}>
            <Text type="supporting" color="secondary">{t("Montant à ajouter")}</Text>
            <HStack gap={1}>
              {BUDGET_PRESETS.map((v) => (
                <Button key={v} label={`+${fmtCompact(v)}`} variant="secondary" size="sm" onClick={() => setAmount(String(v))} />
              ))}
              <TextInput label={t("Montant (tokens)")} isLabelHidden value={amount} onChange={setAmount} placeholder={t("Autre montant...")} size="sm" />
            </HStack>
          </VStack>
          <VStack gap={1}>
            <Text type="supporting" color="secondary">{t("Durée du supplément")}</Text>
            <SegmentedControl value={days} onChange={setDays} label={t("Durée du supplément")} size="sm">
              <SegmentedControlItem value="permanent" label={t("Permanente")} />
              <SegmentedControlItem value="1" label={t("1 j")} />
              <SegmentedControlItem value="3" label={t("3 j")} />
              <SegmentedControlItem value="7" label={t("7 j")} />
              <SegmentedControlItem value="30" label={t("30 j")} />
            </SegmentedControl>
          </VStack>
          {amount && (
            <Text type="supporting" color="secondary">
              {days === "permanent"
                ? `${t("Nouveau total :")} ${fmt(total)}`
                : t("Nouveau total : {total} — retour à {base} le {date} UTC.")
                    .replace("{total}", fmt(total))
                    .replace("{base}", fmt(base))
                    .replace("{date}", expire ? expire.toISOString().slice(0, 16).replace("T", " ") : "")}
            </Text>
          )}
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Confirmer")}
              variant="primary"
              size="sm"
              isDisabled={!amount}
              onClick={() => {
                onApprove(amount, days === "permanent" ? "" : days);
                setIsOpen(false);
                setAmount("");
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}
