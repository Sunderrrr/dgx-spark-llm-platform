"use client";

// Administration — one page, five tabs (option A approved by the operator):
//
//   Vue d'ensemble  state only (platform + live activity) — the 10×-a-day look
//   Modèles         catalog, launches, engine args, backends/sidecars
//   Utilisateurs    accounts, lookups, ceilings, grants
//   Demandes        model + token requests, approve/refuse
//   Système         logs, maintenance, announcement, identity, email, audit
//
// ONE ACTION = ONE TAB: « I want to approve » lands on Demandes, no scrolling.
// This file keeps the DATA LAYER (one 8 s poll, the SSE log stream, the
// sidecar log tails, the single action entry point `act` and its in-flight
// lock); each tab owns only its own forms and display.

import { useCallback, useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Center } from "@astryxdesign/core/Center";
import { VStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { TabList, Tab } from "@astryxdesign/core/TabList";
import { ShieldExclamationIcon } from "@heroicons/react/24/outline";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { authFetch, getJSON, ForbiddenError } from "@/lib/api";
import { useT, tServeur } from "@/lib/i18n";
import type { PlatformStatusData } from "./_components/PlatformStatus";
import { OverviewTab } from "./_components/OverviewTab";
import { ModelsTab } from "./_components/ModelsTab";
import { UsersTab } from "./_components/UsersTab";
import { RequestsTab } from "./_components/RequestsTab";
import { SystemTab } from "./_components/SystemTab";
import { ConfirmActionDialog } from "./_components/ConfirmActionDialog";
import type { LogKind } from "./_components/LogsPanel";
import type { ActResult, AdminData, AuditRow, ConfirmAction } from "./_components/adminTypes";

const MAX_LOG_LINES = 600;

/** The five tabs. The `id` is what the URL carries (`?tab=models`) so an
 *  operator can share a link straight to a tab; unknown ids are ignored and
 *  the page falls back to Vue d'ensemble without an error. */
const TABS = ["overview", "models", "users", "requests", "system"] as const;
type TabId = (typeof TABS)[number];
const DEFAULT_TAB: TabId = "overview";

export default function AdminPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [data, setData] = useState<AdminData | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  // Which model's logs the admin is viewing. "llm" is the live SSE stream
  // (the chat model); the sidecars are fetched on demand + polled. "asr" is
  // the dictation sidecar, served by the same /admin/sidecar-logs route.
  const [logKind, setLogKind] = useState<LogKind>("llm");
  const [sidecarLogs, setSidecarLogs] = useState<string[]>([]);
  const [platform, setPlatform] = useState<PlatformStatusData | null>(null);
  // In-flight lock: during an action (a launch holds the request
  // 10-60 s), ALL the action buttons on the page are disabled — a second
  // click in the meantime can no longer fire twice. The ref duplicates
  // the React state to harden the render window between two clicks.
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);

  // Active tab: read from `?tab=` on load, written back on change via the
  // History API (no reload, no history spam).
  const [tab, setTab] = useState<TabId>(DEFAULT_TAB);
  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get("tab");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot on mount: reading the deep-linked ?tab= from the URL (external system)
    if (raw && (TABS as readonly string[]).includes(raw)) setTab(raw as TabId);
  }, []);
  const selectTab = useCallback((id: string) => {
    const next = (TABS as readonly string[]).includes(id) ? (id as TabId) : DEFAULT_TAB;
    setTab(next);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", next);
    window.history.replaceState(null, "", url.toString());
  }, []);

  function refresh(amorce = false) {
    getJSON<AdminData>("/api/admin")
      .then((d) => {
        setData(d);
        // `amorce`: only copy init_logs on the FIRST load. This refresh
        // runs every 8 s, and it overwrote on every pass the lines accumulated by
        // the SSE stream with a frozen snapshot — so in normal operation the panel
        // lost the whole live feed three times per stream lap, and if the snapshot
        // was empty (cf. runner_logs on the portal side) it was simply emptied.
        // The stream is the source of truth once open; init_logs only fills the
        // panel before its first line.
        if (amorce) setLogs(d.init_logs);
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
  // (200 ok, 400 refusal, 404 not found, 409 to confirm, 503 upstream,
  // 507 memory). So we declare victory ONLY on a 2xx carrying a JSON
  // that does not say ok:false; a non-JSON body, a 4xx/5xx or ok:false are
  // failures. Previously, the catch treated « unreadable JSON » as a
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
      // is not enough to tell a refusal (400/503…) from a success.
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

  const tabProps = { data, act, actionDisabled, setConfirmAction };

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

            {/* The tab labels are spelled out here instead of coming from the
                TABS constant: the i18n check only sees literal translation
                calls. */}
            <TabList value={tab} onChange={selectTab} hasDivider>
              <Tab value="overview" label={t("Vue d'ensemble")} />
              <Tab value="models" label={t("Modèles")} />
              <Tab value="users" label={t("Utilisateurs")} />
              <Tab value="requests" label={t("Demandes")} />
              <Tab value="system" label={t("Système")} />
            </TabList>

            {tab === "overview" && <OverviewTab platform={platform} />}
            {tab === "models" && <ModelsTab {...tabProps} />}
            {tab === "users" && <UsersTab {...tabProps} />}
            {tab === "requests" && <RequestsTab {...tabProps} />}
            {tab === "system" && (
              <SystemTab
                {...tabProps}
                audit={audit}
                logKind={logKind}
                onLogKindChange={(k) => { setSidecarLogs([]); setLogKind(k); }}
                logs={logs}
                sidecarLogs={sidecarLogs}
              />
            )}

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
          </VStack>
        </LayoutContent>
      }
    />
  );
}
