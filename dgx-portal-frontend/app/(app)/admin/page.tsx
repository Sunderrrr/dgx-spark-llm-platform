"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { authFetch, getJSON, postFormVerifie, ForbiddenError } from "@/lib/api";
import { useT, useLocale, tServeur } from "@/lib/i18n";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { UserLookup } from "./_components/UserLookup";
import { EmailConfig } from "./_components/EmailConfig";
import { PlatformStatus, type PlatformStatusData } from "./_components/PlatformStatus";

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
  /** Added in parallel on the portal side; absent from the old backend — the
      row is simply not displayed while the field is missing. */
  asr_model_name?: string | null;
  /** Cause of the dictation model load failure (« CUDA error: out of
      memory », measured on 2026-10-01), published by the sidecar. Absent until
      a load has failed. */
  asr_load_error?: string | null;
  image_status: string;
  image_model_name: string | null;
  image_model_ids: string[];
  music_status: string;
  music_model_name: string | null;
  ocr_cfgs: OcrCfg[];
  voice_cfgs: VoiceCfg[];
};

type CatalogKind = "llm" | "ocr" | "voice" | "video" | "image" | "music";

/** Result of an admin action, as the backend now answers it:
 * {ok: false, error} on refusal, {ok: true, message?, warning?} otherwise. */
type ActResult = { ok: boolean; error?: string; warning?: string };

/** The four destructive/impactful actions that require an explicit confirmation
 * before the POST (cf. ConfirmActionDialog below). */
type ConfirmAction =
  | { kind: "stop-model" }
  | { kind: "launch"; name: string }
  | { kind: "delete-model"; id: number; name: string }
  | { kind: "maintenance" };

const MAX_LOG_LINES = 600;

const SIDECAR_VARIANT: Record<string, "success" | "warning" | "neutral" | "error"> = {
  running: "success",
  starting: "warning",
  // The container runs but its model could not load: it is neither
  // « en ligne » nor « en cours de démarrage », and the wait never ends.
  failed: "error",
  stopped: "neutral",
};
const SIDECAR_LABEL: Record<string, string> = {
  running: "En ligne",
  starting: "Démarrage…",
  failed: "Échec du chargement",
  stopped: "Arrêté",
};

const REQ_STATUS_VARIANT: Record<string, "warning" | "success" | "error"> = { pending: "warning", done: "success", rejected: "error" };
const REQ_STATUS_LABEL: Record<string, string> = { pending: "En attente", done: "Lancé ✓", rejected: "Refusé" };

export default function AdminPage() {
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [data, setData] = useState<AdminData | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  // Which model's logs the admin is viewing. "llm" is the live SSE stream
  // (the chat model); the sidecars are fetched on demand + polled. "asr" is
  // the dictation sidecar, served by the same /admin/sidecar-logs route.
  const [logKind, setLogKind] = useState<"llm" | "ocr" | "voice" | "asr" | "image" | "music" | "video">("llm");
  const [sidecarLogs, setSidecarLogs] = useState<string[]>([]);
  const [newModel, setNewModel] = useState({ name: "", hf_model_id: "", engine: "vllm", vllm_args: "" });
  const [newOcr, setNewOcr] = useState({ name: "", hf_model_id: "", vllm_args: "" });
  const [newVoice, setNewVoice] = useState({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
  const [catalogKind, setCatalogKind] = useState<CatalogKind>("llm");
  const [announce, setAnnounce] = useState({ title: "", body: "" });
  const [settings, setSettings] = useState({ budget: "", duration: "" });
  const [musicModel, setMusicModel] = useState("MiniMaxAI/MiniMax-Music3");
  const [platform, setPlatform] = useState<PlatformStatusData | null>(null);
  // In-flight lock: during an action (a launch holds the request
  // 10-60 s), ALL the action buttons on the page are disabled — a second
  // click in the meantime can no longer fire twice. The ref duplicates
  // the React state to harden the render window between two clicks.
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);

  // Text displayed in the log viewer, and automatic stick-to-bottom —
  // same behaviour as the Playground panel: stick to the bottom as long as
  // the admin has not scrolled up; if they do, we stop pulling them back down and
  // an arrow appears to go back down and re-enable the tracking. `active` is
  // always true here (unlike the Playground where it only tracks during the
  // stream): logs keep arriving as long as the page is open.
  // The join of up to 600 lines was redone at EVERY render — including an
  // unrelated keystroke or an unchanged poll.
  const logText = useMemo(
    () => (logKind === "llm" ? logs : sidecarLogs).join("\n"),
    [logKind, logs, sidecarLogs],
  );
  const {
    setRef: attachLogsScroller,
    showButton: showLogsJump,
    scrollToBottom: logsJumpDown,
  } = useStickToBottom(logText, true);

  // CodeBlock manages its own scrolling (as soon as it is given a maxHeight) and
  // does not expose this container. So we put the ref on a parent and go down
  // to the actually scrollable element — verified in the browser: without this, the
  // text is clipped by an overflow:hidden child and nothing scrolls anymore.
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

  // `amorce`: only copy init_logs on the FIRST load. This refresh
  // runs every 8 s, and it overwrote on every pass the lines accumulated by
  // the SSE stream with a frozen snapshot — so in normal operation the panel
  // lost the whole live feed three times per stream lap, and if the snapshot
  // was empty (cf. runner_logs on the portal side) it was simply emptied.
  // The stream is the source of truth once open; init_logs only fills the
  // panel before its first line.
  function refresh(amorce = false) {
    getJSON<AdminData>("/api/admin")
      .then((d) => {
        setData(d);
        if (amorce) {
          setLogs(d.init_logs);
          // The default quotas form is pre-filled only on the FIRST
          // load: re-syncing it on every poll pass
          // overwrote the admin's input under their fingers, and it was the stale
          // value that was sent on the click on « Appliquer ».
          setSettings({ budget: String(d.default_key_budget), duration: d.default_key_duration });
        }
      })
      .catch((e) => {
        if (e instanceof ForbiddenError) setForbidden(true);
      });
    // Audit log + platform snapshot: in the same refresh
    // loop (8 s, and after each action via act()), so that the
    // log shows the action just taken instead of a snapshot
    // of the page load. Two extra requests per pass, local and
    // light — the accepted trade-off of the admin screen.
    getJSON<AuditRow[]>("/admin/audit")
      .then((d) => setAudit(d ?? []))
      .catch(() => {});
    getJSON<PlatformStatusData>("/admin/platform")
      .then((d) => setPlatform(d))
      .catch(() => setPlatform(null));
  }

  useEffect(() => { refresh(true); }, []);
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
    // The stream replays its WHOLE buffer at each connection: so we clear on
    // open, otherwise its lines would add to those of the seed and the
    // panel would show everything twice.
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

  // The backend contract (extended to ALL admin action routes):
  // JSON {ok: boolean, error?, message?, warning?} with an honest HTTP code
  // (200 ok, 400 refusal, 404 not found, 409 to confirm, 502 upstream,
  // 507 memory). So we declare victory ONLY on a 2xx carrying a JSON
  // that does not say ok:false; a non-JSON body, a 4xx/5xx or ok:false are
  // failures. Previously, the catch treated « JSON illisible » as a
  // success: a refused stop was displayed as done. The server sentences
  // (error/message/warning) are free French, not i18n keys — they are
  // displayed as-is.
  async function act(url: string, params: Record<string, string> = {}): Promise<ActResult> {
    if (!csrf || busyRef.current) return { ok: false };
    busyRef.current = true;
    setBusy(true);
    let result: ActResult = { ok: false };
    try {
      // authFetch (not postFormJSON) to read the HTTP code: JSON alone
      // is not enough to tell a refusal (400/502…) from a success.
      const res = await authFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
        body: new URLSearchParams(params).toString(),
      });
      const body = await res.json().catch(() => null) as (ActResult & { message?: string }) | null;
      const warning = typeof body?.warning === "string" ? body.warning : undefined;
      if (res.status === 403) {
        // Admin demoted mid-session: « accès refusé », never a
        // success. (authFetch does not throw on 403 — only 401 redirects.)
        showToast({ body: t("Accès refusé."), type: "error" });
      } else if (res.status >= 400 || !body || body.ok === false) {
        const errMsg = body?.error ? tServeur(body.error, t) : t("Échec de l'action.");
        showToast({ body: errMsg, type: "error" });
        result = { ok: false, error: errMsg, warning };
      } else {
        // The server often provides a useful sentence (« Lancement de X
        // accepté — chargement en cours. »): we display it preferentially
        // (translated at display time, cf. tServeur).
        showToast({ body: body.message ? tServeur(body.message, t) : t("Action effectuée."), type: "info" });
        // PARTIAL success (files not deleted, LiteLLM not deregistered…): the
        // server writes it in `warning`, which was displayed nowhere.
        if (warning) showToast({ body: tServeur(warning, t), type: "error" });
        result = { ok: true, warning };
      }
    } catch {
      // Network error (no answer at all): failure, obviously.
      showToast({ body: t("Échec de l'action."), type: "error" });
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
    refresh();
    return result;
  }

  // The four confirmations go through a single dialog (ConfirmActionDialog):
  // the click on the action button only opens the dialog, the POST is only sent
  // on confirmation. For the deletion, `confirm=1` accompanies the confirmed
  // POST (the backend requires it to remove the currently served model entry —
  // the UI confirmation is precisely that consent).
  function runConfirmedAction(action: ConfirmAction) {
    if (action.kind === "stop-model") act("/admin/model/stop");
    else if (action.kind === "launch") act("/admin/model/launch", { model_name: action.name });
    else if (action.kind === "delete-model") act(`/admin/model/delete/${action.id}`, { confirm: "1" });
    else act("/admin/maintenance/toggle");
  }

  const actionDisabled = busy || !csrf;

  const st = data?.v_status.status;

  const budgetColumns: TableColumn<BudgetRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: t("Utilisateur"), renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "key_alias", header: t("Clé") },
    { key: "current_budget", header: t("Budget actuel"), renderCell: (r) => (r.current_budget ? Math.round(r.current_budget).toLocaleString(numLocale) : "—") },
    { key: "reason", header: t("Raison"), renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    {
      key: "status",
      header: t("Statut"),
      renderCell: (r) =>
        r.status === "pending" ? (
          <Badge label={t("En attente")} variant="warning" />
        ) : r.status === "approved" ? (
          <Badge label={`+${Math.round(r.granted_amount || 0).toLocaleString(numLocale)} ✓`} variant="success" />
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
            <BudgetApproveForm fullname={r.fullname} currentBudget={r.current_budget} isDisabled={actionDisabled} onApprove={(amount, days) => act(`/admin/budget/approve/${r.id}`, { amount, grant_days: days })} />
            <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/budget/reject/${r.id}`)} />
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
          {r.status !== "done" && <Button label={t("Lancé")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={CheckIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/update/${r.id}`, { status: "done" })} />}
          {r.status !== "rejected" && <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/update/${r.id}`, { status: "rejected" })} />}
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

            {/* Dashboard widget (disk, backup, monitor, served
                model, counters): refreshed by the same 8 s poll as the
                rest of the page — the data flows down, no second timer. */}
            <PlatformStatus status={platform} />

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
                    isDisabled={actionDisabled}
                    onClick={() => setConfirmAction({ kind: "maintenance" })}
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
                          // Stopping the served model cuts ALL generations
                          // in flight: that deserves a dialog, not a direct click.
                          <Button
                            label={t("Arrêter")}
                            variant="secondary"
                            size="sm"
                            isIconOnly
                            icon={<Icon icon={StopIcon} size="sm" />}
                            isDisabled={actionDisabled}
                            onClick={() => setConfirmAction({ kind: "stop-model" })}
                          />
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
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/start")} />
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
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/video/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/video/start")} />
                        )}
                      </HStack>
                      {/* The video is a frozen ComfyUI workflow: the load
                          exposes no model name, we do not invent one. */}
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
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/start")} />
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
                        {data.asr_status === "running" || data.asr_status === "starting"
                         || data.asr_status === "failed" ? (
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/asr/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/asr/start")} />
                        )}
                      </HStack>
                      {/* The asr model name is only provided if the backend
                          reports it (asr_model_name, added in parallel): without
                          it, no row at all — never a hardcoded name. */}
                      {data.asr_model_name && (
                        <Text type="supporting" color="secondary" wordBreak="break-all">{data.asr_model_name}</Text>
                      )}
                      {/* The container runs WITHOUT a loaded model: it is not a
                          startup in progress but a failure, and the sidecar
                          publishes its cause (not enough unified memory on
                          2026-10-01). The button stays « Arrêter » — the
                          container holds the port — hence the reminder of the
                          order: stop, free, relaunch. */}
                      {data.asr_load_error && (
                        <>
                          <Text type="supporting" color="secondary" wordBreak="break-all">
                            {t("Le modèle de dictée n'a pas pu se charger :")} {data.asr_load_error}
                          </Text>
                          <Text type="supporting" color="secondary">
                            {t("La mémoire est partagée avec le modèle de chat : libère de la mémoire, puis relance la dictée.")}
                          </Text>
                        </>
                      )}
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
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/start")} />
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
                          <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/music/stop")} />
                        ) : (
                          <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/music/start")} />
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
                          {/* Lancer = load the model into memory (minutes,
                              heavy memory footprint) and Supprimer = remove
                              the entry (from the catalog AND the LiteLLM
                              routing): both go through a confirmation. */}
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => setConfirmAction({ kind: "launch", name: cfg.name })} />
                          <ArgsEditForm
                            cfg={cfg}
                            isDisabled={actionDisabled}
                            onSubmit={(params) => act(`/admin/model/edit/${cfg.id}`, params)}
                          />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => setConfirmAction({ kind: "delete-model", id: cfg.id, name: cfg.name })} />
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
                        isDisabled={actionDisabled}
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
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/catalog/launch", { ocr_name: cfg.name })} />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/ocr/catalog/delete/${cfg.id}`)} />
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
                        isDisabled={actionDisabled}
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
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/catalog/launch", { voice_name: cfg.name })} />
                          <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/voice/catalog/delete/${cfg.id}`)} />
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
                          { value: "Qwen3-TTS-12Hz-1.7B-Base", label: t("Qwen3-TTS 1.7B (10 langues)") },
                          { value: "Qwen3-TTS-12Hz-0.6B-Base", label: t("Qwen3-TTS 0.6B (10 langues)") },
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
                        isDisabled={actionDisabled}
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
                          <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/launch", { model_id: mid })} />
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
                        isDisabled={actionDisabled}
                        onClick={() => act("/admin/music/launch", { model_id: musicModel.trim() })}
                      />
                    </VStack>
                  </Card>
                </Grid>
              )}

              {data && catalogKind === "video" && (
                <Card>
                  <VStack gap={1}>
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
                      isDisabled={actionDisabled}
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
                        ? t("Logs — {v}").replace("{v}", data?.v_status.model || t("aucun modèle"))
                        : t("Logs — {v}").replace("{v}", logKind.toUpperCase())}
                    </Text>
                    <SegmentedControl label={t("Logs à afficher")} value={logKind} onChange={(v) => { setSidecarLogs([]); setLogKind(v as typeof logKind); }}>
                      <SegmentedControlItem value="llm" label={t("LLM")} />
                      <SegmentedControlItem value="ocr" label="OCR" />
                      <SegmentedControlItem value="voice" label={t("Voix")} />
                      <SegmentedControlItem value="asr" label={t("Dictée")} />
                      <SegmentedControlItem value="image" label={t("Image")} />
                      <SegmentedControlItem value="music" label={t("Musique")} />
                      <SegmentedControlItem value="video" label={t("Vidéo")} />
                    </SegmentedControl>
                  </HStack>
                  {/* The maxHeight hands the scrolling to CodeBlock: constraining a
                      parent instead does not work, CodeBlock gets compressed and
                      clips its text (internal overflow:hidden), so that nothing
                      overflows or scrolls anymore. The ref only serves as an entry
                      point to find its scroller. The container is position
                      relative to anchor the arrow INSIDE the frame. */}
                  <VStack ref={setLogsScrollRef} style={{ position: "relative" }}>
                    <CodeBlock
                      code={logText || t("Aucun log — ce modèle n'est pas démarré.")}
                      language="plaintext"
                      hasCopyButton
                      width="100%"
                      maxHeight={280}
                    />
                    {/* Arrow alone, centered at the bottom of the frame (the top-right
                        corner is already taken by the Copier button of CodeBlock). */}
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
                    isDisabled={actionDisabled}
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
                        .replace("{total}", Math.round(g.current_budget).toLocaleString(numLocale))
                        .replace("{date}", g.expires_at.slice(0, 16).replace("T", " "))
                        .replace("{base}", Math.round(g.base_budget).toLocaleString(numLocale))}
                    </Text>
                  ))}
                </VStack>
              )}
              <BudgetSetForm rows={data?.spend_data ?? []} actionDisabled={actionDisabled} />
            </VStack>

            <UserLookup
              spend={data?.spend_data ?? []}
              ocr={data?.ocr_usage ?? []}
              video={data?.video_usage ?? []}
              voice={data?.voice_usage ?? []}
              requests={data?.requests ?? []}
            />

            {/* A single instance for the four confirmations (model stop,
                launch, catalog deletion, maintenance): content driven
                by confirmAction. */}
            <ConfirmActionDialog
              action={confirmAction}
              maintenanceActive={!!data?.maintenance_mode}
              isDisabled={actionDisabled}
              onClose={() => setConfirmAction(null)}
              onConfirm={runConfirmedAction}
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

/** « 10M » rather than « 10 000 000 » on the buttons: readable at a glance.
    Amounts that do not round cleanly stay spelled out. */
const fmtCompact = (n: number, numLocale: string) =>
  n >= 1_000_000 && n % 1_000_000 === 0 ? `${Math.round(n / 1_000_000)}M` : Math.round(n).toLocaleString(numLocale);

function BudgetSetForm({ rows, actionDisabled }: { rows: SpendRow[]; actionDisabled?: boolean }) {
  /** Reset an account's cap: EXACT amount (not an addition) — just as useful
     to LOWER a quota (200M → 50M) as to raise it. The user is
     CHOSEN from a list (with their current cap displayed): no name to
     type blindly. */
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [user, setUser] = useState("");
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);
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
            label: `${r.username} · ${r.unlimited ? t("illimité") : fmtCompact(r.max_budget || 0, numLocale)}`,
          }))}
          value={user}
          onChange={setUser}
        />
        {sel && (
          <Text type="supporting" color="secondary">
            {t("Plafond actuel :")} {sel.unlimited ? t("Illimitée (admin)") : fmtCompact(sel.max_budget || 0, numLocale)}
          </Text>
        )}
        <HStack gap={1}>
          {[50000000, 100000000, 200000000].map((v) => (
            <Button key={v} label={fmtCompact(v, numLocale)} variant="secondary" size="sm" onClick={() => setBudget(String(v))} />
          ))}
        </HStack>
        <HStack gap={2} vAlign="end">
          <TextInput label={t("Nouveau plafond (tokens)")} value={budget} onChange={setBudget} placeholder="50000000" size="sm" />
          <Button
            label={t("Redéfinir")}
            variant="primary"
            size="sm"
            isDisabled={!user || !budget || busy || actionDisabled}
            onClick={async () => {
              setBusy(true);
              try {
                // The verdict is READ: the route explicitly refuses (400) an
                // absurd cap (0, negative, > 1e12 — the hole of a 6.7e12
                // typed amount). With `postForm` alone, this refusal displayed
                // « Budget redéfini. ».
                const res = await postFormVerifie(
                  `/admin/users/${encodeURIComponent(user.trim())}/budget/set`,
                  csrf, { budget: budget.trim() },
                );
                if (!res.ok) {
                  showToast({ body: res.error ? tServeur(res.error, t) : t("L'action a échoué."), type: "error" });
                  return;
                }
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

function BudgetApproveForm({ onApprove, fullname, currentBudget, isDisabled }: {
  onApprove: (amount: string, days: string) => void;
  fullname: string;
  currentBudget: number | null;
  /** During a whole-page action (or without a CSRF token): neither the opening of
      the dialog nor its confirmation must be sent. */
  isDisabled?: boolean;
}) {
  /** ONE button per request: « Approuver » opens a dialog with one-click amounts
     (+10M/+50M/+100M), a duration in segments (Permanente/1j/3j/7j/30j)
     and a preview of the result BEFORE confirming. No field left to guess. */
  const t = useT();
  const numLocale = useLocale();
  const [isOpen, setIsOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [days, setDays] = useState("permanent");
  const fmt = (n: number) => Math.round(n).toLocaleString(numLocale);
  const base = currentBudget || 0;
  const total = base + (parseFloat(amount) || 0);
  // eslint-disable-next-line react-hooks/purity -- « retour à la base le … » preview: the date is by nature relative to now, recomputed at each opening of the dialog
  const expire = days === "permanent" ? null : new Date(Date.now() + parseInt(days, 10) * 86400000);
  return (
    <>
      <Button label={t("Approuver")} variant="ghost" size="sm" isDisabled={isDisabled} onClick={() => setIsOpen(true)} />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Accorder des tokens")} subtitle={`${fullname} — ${t("Actuel :")} ${fmt(base)}`} />
        <VStack gap={3}>
          <VStack gap={1}>
            <Text type="supporting" color="secondary">{t("Montant à ajouter")}</Text>
            <HStack gap={1}>
              {BUDGET_PRESETS.map((v) => (
                <Button key={v} label={`+${fmtCompact(v, numLocale)}`} variant="secondary" size="sm" onClick={() => setAmount(String(v))} />
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
              isDisabled={!amount || isDisabled}
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

/** Confirmation dialog for the four consequential actions (stopping the
 * served model, launch, catalog deletion, maintenance toggle). A
 * consequence sentence, a confirmation button (destructive for what
 * cuts/destroys, primary for the launch), an Annuler button — the POST is
 * only sent on the click on Confirmer. */
function ConfirmActionDialog({ action, maintenanceActive, isDisabled, onClose, onConfirm }: {
  action: ConfirmAction | null;
  maintenanceActive: boolean;
  isDisabled: boolean;
  onClose: () => void;
  onConfirm: (action: ConfirmAction) => void;
}) {
  const t = useT();
  if (!action) return null;
  let title: string;
  let body: string;
  let confirmLabel: string;
  let confirmVariant: "primary" | "secondary" | "destructive";
  if (action.kind === "stop-model") {
    title = t("Arrêter le modèle servi ?");
    body = t("Le modèle va être coupé : toutes les générations en cours, pour tous les utilisateurs, seront interrompues.");
    confirmLabel = t("Arrêter");
    confirmVariant = "destructive";
  } else if (action.kind === "launch") {
    title = t("Lancer {name} ?").replace("{name}", action.name);
    body = t("Le modèle sera chargé en mémoire unifiée — le lancement peut prendre plusieurs minutes.");
    confirmLabel = t("Lancer");
    confirmVariant = "primary";
  } else if (action.kind === "delete-model") {
    title = t("Supprimer {name} ?").replace("{name}", action.name);
    body = t("Le modèle sera retiré du catalogue et du routage LiteLLM, et ses fichiers seront effacés du disque (sauf s'ils servent à un autre modèle). Pour le retélécharger, il faudra le réinstaller. Un modèle en cours doit d'abord être arrêté.");
    confirmLabel = t("Supprimer");
    confirmVariant = "destructive";
  } else if (maintenanceActive) {
    title = t("Désactiver le mode maintenance ?");
    body = t("Le trafic non-admin vers les routes de génération sera de nouveau accepté.");
    confirmLabel = t("Désactiver");
    confirmVariant = "secondary";
  } else {
    title = t("Activer le mode maintenance ?");
    body = t("Le trafic non-admin vers les routes de génération sera refusé ; les admins gardent l'accès.");
    confirmLabel = t("Activer");
    confirmVariant = "secondary";
  }
  return (
    <Dialog isOpen onOpenChange={(o) => { if (!o) onClose(); }} purpose="form">
      <DialogHeader title={title} />
      <VStack gap={3}>
        <Text type="supporting" color="secondary">{body}</Text>
        <HStack gap={2} hAlign="end">
          <Button label={t("Annuler")} variant="ghost" size="sm" onClick={onClose} />
          <Button
            label={confirmLabel}
            variant={confirmVariant}
            size="sm"
            isDisabled={isDisabled}
            onClick={() => { onConfirm(action); onClose(); }}
          />
        </HStack>
      </VStack>
    </Dialog>
  );
}

/** Make the args of a catalog entry editable (POST
 * /admin/model/edit/<id>): they were frozen at creation time. Name, HF ID and
 * engine are displayed read-only — they are what identifies the entry,
 * the route only changes the args. The backend `warning` field (LiteLLM
 * routing not refreshed) is shown when present. */
function ArgsEditForm({ cfg, isDisabled, onSubmit }: {
  cfg: ModelCfg;
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<ActResult>;
}) {
  const t = useT();
  const showToast = useToast();
  const [isOpen, setIsOpen] = useState(false);
  const [args, setArgs] = useState(cfg.vllm_args);
  return (
    <>
      <Button
        label={t("Args")}
        variant="secondary"
        size="sm"
        isDisabled={isDisabled}
        onClick={() => { setArgs(cfg.vllm_args); setIsOpen(true); }}
      />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Modifier les args du moteur")} subtitle={`${cfg.name} · ${cfg.hf_model_id} · ${cfg.engine}`} />
        <VStack gap={3}>
          <Text type="supporting" color="secondary">{t("Seuls les args du moteur changent ; l'entrée reste identifiée par son nom et son HF ID.")}</Text>
          <TextInput label={t("Args du moteur")} value={args} onChange={setArgs} size="sm" />
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Enregistrer")}
              variant="primary"
              size="sm"
              isDisabled={isDisabled}
              onClick={async () => {
                const res = await onSubmit({
                  name: cfg.name,
                  hf_model_id: cfg.hf_model_id,
                  vllm_args: args,
                  engine: cfg.engine,
                });
                if (res.ok) {
                  setIsOpen(false);
                  // The args are saved but LiteLLM did not follow:
                  // the entry stays routed with old context limits.
                  if (res.warning) showToast({ body: res.warning, type: "error" });
                }
                // On failure, the dialog stays open (the error is already toasted
                // by act()): the admin can fix and resubmit.
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}
