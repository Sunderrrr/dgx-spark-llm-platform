"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Icon } from "@astryxdesign/core/Icon";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { Pagination } from "@astryxdesign/core/Pagination";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import type { DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { useToast } from "@astryxdesign/core/Toast";
import {
  PlusIcon,
  UserPlusIcon,
  KeyIcon,
  TrashIcon,
  ShieldCheckIcon,
  NoSymbolIcon,
  CheckCircleIcon,
  UsersIcon,
  EyeIcon,
  ArchiveBoxXMarkIcon,
  PencilSquareIcon,
} from "@heroicons/react/24/outline";
import { getJSON, postFormJSON, sendJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { UserAvatar } from "@/lib/user-avatar";

import {
  DIRECTORY_STATUS_META,
  SOURCE_META,
  quotaPeriod,
  type AdminUserDetail,
  type LocalUser,
  type UsersData,
} from "./users/shared";
import { UserCreateDialog } from "./users/UserCreateDialog";
import { GroupCreateDialog } from "./users/GroupCreateDialog";
import { PasswordResetDialog } from "./users/PasswordResetDialog";
import { BlockAccountDialog } from "./users/BlockAccountDialog";
import { DeleteAccountDialog } from "./users/DeleteAccountDialog";
import { PurgeAccountDialog } from "./users/PurgeAccountDialog";
import { UserDetailDialog } from "./users/UserDetailDialog";

// Pagination of the users table (client-side).
const PAGE_SIZE = 10;

// Compact stat tile used in the overview row (module-level so it isn't
// recreated on every render).
function Tile({ icon, value, label, locale }: { icon: typeof UsersIcon; value: number; label: string; locale: string }) {  return (
    <Card>
      <HStack gap={3} vAlign="center">
        <Icon icon={icon} size="md" color="secondary" />
        <VStack gap={0}>
          <Text size="xl" weight="bold" hasTabularNumbers>{value.toLocaleString(locale)}</Text>
          <Text type="supporting" color="secondary">{label}</Text>
        </VStack>
      </HStack>
    </Card>
  );
}

export function UsersSection({
  csrf,
  initial,
  initialFresh = false,
}: {
  csrf: string;
  /** Read by the server (users page): shown at once. Absent (admin tab): fetched. */
  initial?: UsersData | null;
  /** True when `initial` was read for THIS visit — the mount fetch is then
   *  skipped. False for a router-cache replay: shown, then refreshed. */
  initialFresh?: boolean;
}) {
  const t = useT();
  const showToast = useToast();
  const numLocale = useLocale();
  const [data, setData] = useState<UsersData | null>(initial ?? null);

  // Toolbar state: free-text search + auth-source filter.
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  // Loading state (first fetch) + current page of the table.
  const [page, setPage] = useState(1);

  // Dialog state — create forms and the (masked) password reset live in modals
  // instead of always-open forms / window.prompt.
  const [userDialog, setUserDialog] = useState(false);
  const [groupDialog, setGroupDialog] = useState(false);
  const [pwUser, setPwUser] = useState<LocalUser | null>(null);
  const [pw, setPw] = useState("");
  const [nu, setNu] = useState({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
  const [ng, setNg] = useState({ name: "", max_budget: "", is_admin: "0" });
  const [editedGroup, setEditedGroup] = useState(false);
  // Blocking an account (reason requested, optional).
  const [blockUser, setBlockUser] = useState<LocalUser | null>(null);
  const [blockReason, setBlockReason] = useState("");
  const [blockBusy, setBlockBusy] = useState(false);
  // Deletion: destructive, confirmation by typing DELETE.
  const [delUser, setDelUser] = useState<LocalUser | null>(null);
  const [delConfirm, setDelConfirm] = useState("");
  const [delBusy, setDelBusy] = useState(false);
  const [delError, setDelError] = useState<string | null>(null);
  // Success warning ({ok:true, warning}): some keys could not be
  // revoked on the LiteLLM side — displayed next to the success, not as a failure.
  const [delWarning, setDelWarning] = useState<string | null>(null);
  // Purge of a directory account's data (LDAP/SSO, no local row):
  // erases its data WITHOUT removing access — offboarding is Bloquer.
  const [purgeUser, setPurgeUser] = useState<LocalUser | null>(null);
  const [purgeConfirm, setPurgeConfirm] = useState("");
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  // Account detail (drawer): loaded on opening.
  const [detailUser, setDetailUser] = useState<LocalUser | null>(null);
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [sessRevoking, setSessRevoking] = useState(false);
  // Warning returned by create/update ({ok:true, warning}): the LiteLLM
  // quota could not be applied, the account is temporarily uncapped.
  // The dialog stays open to read it, a Banner shows it at the top.
  const [formWarning, setFormWarning] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getJSON<UsersData>("/api/admin/users").then(setData).catch(() => {});
  }, []);
  const alreadyLoaded = useRef(!!initial && initialFresh);
  useEffect(() => {
    if (alreadyLoaded.current) {
      alreadyLoaded.current = false;
      return;
    }
    refresh();
  }, [refresh]);

  // Returns false on failure, otherwise {ok:true, warning?}. `warning`
  // (LiteLLM quota not applied…) accompanies a SUCCESS: it is displayed
  // prominently in the dialog, not as an error.
  const act = useCallback(
    async (url: string, params: Record<string, string>): Promise<{ ok: true; warning?: string } | false> => {
      if (!csrf) return false;
      let warning: string | undefined;
      try {
        const res = await postFormJSON<{ ok?: boolean; error?: string; warning?: string }>(url, csrf, params);
        if (res && res.ok === false) {
          showToast({ body: res.error ? t(res.error) : t("Échec de l'action."), type: "error" });
          return false;
        }
        warning = res?.warning;
      } catch {
        showToast({ body: t("Échec de l'action."), type: "error" });
        return false;
      }
      showToast({ body: t("Action effectuée."), type: "info" });
      refresh();
      return { ok: true, warning };
    },
    [csrf, refresh, showToast, t],
  );

  // Like act(), but to the new JSON routes ({confirm, reason}…) —
  // {ok:false, error} errors go through t() as in act(): an
  // unknown server sentence falls back to itself (silent fallback).
  // Returns the response or null, the caller picks its success toast.
  const actJSON = useCallback(
    async <T extends { ok?: boolean; error?: string }>(url: string, body?: unknown): Promise<T | null> => {
      if (!csrf) return null;
      try {
        const res = await sendJSON<T>(url, csrf, body);
        if (res && res.ok === false) {
          showToast({ body: res.error ? t(res.error) : t("Échec de l'action."), type: "error" });
          return null;
        }
        return res;
      } catch {
        showToast({ body: t("Échec de l'action."), type: "error" });
        return null;
      }
    },
    [csrf, showToast, t],
  );

  const fmtBudget = (n: number) => `${Math.round(n).toLocaleString(numLocale)}`;

  // Detail: `sources` is a « ldap,sso » string — we split it for the badges.
  const detailSources = detail?.sources
    ? detail.sources.split(",").map((s) => s.trim()).filter(Boolean)
    : [];

  const users = useMemo(() => data?.users ?? [], [data]);
  const groups = data?.groups ?? [];
  // The quota period (« / semaine »): every cap on this page is per period.
  const p = quotaPeriod(data?.budget_duration);
  const period = t(p.msgid).replace("{n}", String(p.n ?? ""));

  // Overview counters for the stat tiles.
  const stats = useMemo(() => {
    // A SINGLE pass: four full `filter` runs over the same list, on top of
    // the `filtered` one, made five reads for a single piece of information.
    let local = 0, ldap = 0, sso = 0, admins = 0;
    for (const u of users) {
      if (u.managed) local++;
      if (u.sources.includes("ldap")) ldap++;
      if (u.sources.includes("sso")) sso++;
      if (u.effective_admin) admins++;
    }
    return { total: users.length, local, ldap, sso, admins };
  }, [users]);

  // Apply search + source filter.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return users.filter((u) => {
      const matchesQ = !q || u.username.toLowerCase().includes(q) || !!u.fullname?.toLowerCase().includes(q);
      const srcs = u.sources.length ? u.sources : ["external"];
      const matchesSrc = sourceFilter === "all" || srcs.includes(sourceFilter);
      return matchesQ && matchesSrc;
    });
  }, [users, query, sourceFilter]);

  // Slicing of the current page (client-side pagination).
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageUsers = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const groupOptions = [
    { label: t("Aucun groupe"), value: "" },
    ...groups.map((g) => ({ label: g.name, value: g.name })),
  ];
  const sourceOptions = [
    { label: t("Toutes les sources"), value: "all" },
    { label: t("Local"), value: "local" },
    { label: "LDAP", value: "ldap" },
    { label: "SSO", value: "sso" },
    { label: t("Externe"), value: "external" },
  ];

  async function createUser() {
    const res = await act("/admin/users/create", { ...nu });
    if (!res) return;
    setNu({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
    if (res.warning) setFormWarning(res.warning); // dialog left open to read the warning
    else { setFormWarning(null); setUserDialog(false); }
  }
  async function createGroup() {
    const res = await act("/admin/groups/create", { ...ng });
    if (!res) return;
    setNg({ name: "", max_budget: "", is_admin: "0" });
    if (res.warning) setFormWarning(res.warning);
    else { setFormWarning(null); setGroupDialog(false); }
  }
  async function submitPassword() {
    if (!pwUser || pw.length < 8) return;
    const res = await act(`/admin/users/update/${pwUser.id}`, { password: pw });
    if (!res) return;
    setPw("");
    if (res.warning) setFormWarning(res.warning);
    else { setFormWarning(null); setPwUser(null); }
  }

  // Account detail: (re)loaded on opening and after an action that modifies
  // it (session revocation) — always from a handler, not an effect: the
  // « détail » state does not have to sync itself.
  const loadDetail = useCallback((username: string) => {
    setDetailLoading(true);
    setDetailError(null);
    getJSON<AdminUserDetail>(`/admin/users/${encodeURIComponent(username)}/detail`)
      .then((d) => setDetail(d))
      .catch(() => setDetailError(t("Chargement impossible.")))
      .finally(() => setDetailLoading(false));
  }, [t]);

  function openDetail(u: LocalUser) {
    setDetail(null);
    setDetailUser(u);
    loadDetail(u.username);
  }

  function openDelete(u: LocalUser) {
    setDelUser(u);
    setDelConfirm("");
    setDelError(null);
    setDelWarning(null);
  }

  function openPurge(u: LocalUser) {
    setPurgeUser(u);
    setPurgeConfirm("");
    setPurgeError(null);
  }

  // Purge of a directory account: data erased, access kept. {ok:false, error}
  // errors (400/409) displayed verbatim in the dialog.
  async function submitPurge() {
    if (!purgeUser || purgeBusy || purgeConfirm !== "DELETE") return;
    setPurgeBusy(true);
    const res = await sendJSON<{ ok?: boolean; error?: string }>(
      `/admin/users/${encodeURIComponent(purgeUser.username)}/purge`, csrf, { confirm: "DELETE" });
    setPurgeBusy(false);
    if (res && res.ok === false) {
      setPurgeError(res.error || t("Échec de l'action."));
      return;
    }
    setPurgeUser(null);
    setPurgeConfirm("");
    setPurgeError(null);
    showToast({ body: t("Données du compte purgées."), type: "info" });
    refresh();
  }

  async function submitBlock() {
    if (!blockUser || blockBusy) return;
    setBlockBusy(true);
    // Optional reason: we only send it if the admin typed one.
    const reason = blockReason.trim();
    const res = await actJSON<{ ok?: boolean; revoked_sessions?: number; error?: string }>(
      `/admin/users/${encodeURIComponent(blockUser.username)}/block`, reason ? { reason } : {});
    setBlockBusy(false);
    if (!res) return;
    setBlockUser(null);
    setBlockReason("");
    showToast({
      body: res.revoked_sessions
        ? t("Compte bloqué — {n} session(s) révoquée(s).").replace("{n}", String(res.revoked_sessions))
        : t("Compte bloqué."),
      type: "info",
    });
    refresh();
  }

  async function unblockUser(u: LocalUser) {
    const res = await actJSON(`/admin/users/${encodeURIComponent(u.username)}/unblock`, {});
    if (!res) return;
    showToast({ body: t("Compte débloqué."), type: "info" });
    refresh();
  }

  async function revokeDetailSessions() {
    if (!detailUser || sessRevoking) return;
    setSessRevoking(true);
    const res = await actJSON(`/admin/users/${encodeURIComponent(detailUser.username)}/revoke-sessions`, {});
    setSessRevoking(false);
    if (!res) return;
    showToast({ body: t("Sessions révoquées."), type: "info" });
    loadDetail(detailUser.username);
  }

  async function submitDelete() {
    if (!delUser || delBusy || delConfirm !== "DELETE") return;
    setDelBusy(true);
    // The {"confirm": "DELETE"} body is required by the route; a
    // {ok:false, error} error is displayed verbatim in the dialog.
    const res = await sendJSON<{ ok?: boolean; error?: string; warning?: string }>(
      `/admin/users/delete/${delUser.id}`, csrf, { confirm: "DELETE" });
    setDelBusy(false);
    if (res && res.ok === false) {
      setDelError(res.error || t("Échec de l'action."));
      return;
    }
    showToast({ body: t("Compte supprimé."), type: "info" });
    refresh();
    // Success warning (keys not revoked on the LiteLLM side): the dialog stays
    // open to show it — never a silent security failure.
    if (res?.warning) {
      setDelConfirm("");
      setDelWarning(res.warning);
      return;
    }
    setDelUser(null);
    setDelConfirm("");
    setDelError(null);
    setDelWarning(null);
  }

  const columns: TableColumn<LocalUser & Record<string, unknown>>[] = [
    { key: "username", header: t("Utilisateur"), renderCell: (u) => (
        <HStack gap={2} vAlign="center">
          <UserAvatar
            avatarId={u.avatar_id}
            username={u.username}
            name={u.fullname || u.username}
            size="sm"
          />
          <VStack gap={0}>
            <Text weight="semibold">{u.username}</Text>
            {u.fullname ? <Text type="supporting" color="secondary">{u.fullname}</Text> : null}
            {/* The instance's owner: the other admins cannot touch it. On its
                own line: next to the name it split « mdupont » in two. */}
            {u.owner ? <HStack><Badge label={t("Propriétaire")} variant="info" /></HStack> : null}
          </VStack>
        </HStack>
      ) },
    { key: "sources", header: t("Source"), renderCell: (u) => (
        <HStack gap={1} wrap="wrap">
          {(u.sources.length ? u.sources : ["external"]).map((s) => {
            const m = SOURCE_META[s] ?? { label: s, variant: "neutral" as const };
            return <Badge key={s} label={t(m.label)} variant={m.variant} />;
          })}
        </HStack>
      ) },
    { key: "managed", header: t("Géré"), renderCell: (u) =>
        u.managed_by === "local" ? <Badge label={t("Ici")} variant="green" /> : <Badge label="Authentik" variant="neutral" /> },
    { key: "group_name", header: t("Groupe"), renderCell: (u) => {
        // A local account has one portal group; an LDAP/SSO account the groups
        // its directory gives it (merged by name).
        const names = u.group_name ? [u.group_name] : (u.directory_groups ?? []);
        return names.length ? <Text maxLines={2}>{names.join(", ")}</Text> : "—";
      } },
    { key: "effective_budget", header: `${t("Quota")} ${period}`, renderCell: (u) =>
        u.unlimited ? <Text color="secondary">{t("Illimité")}</Text>
        : u.effective_budget != null ? <Text hasTabularNumbers>{fmtBudget(u.effective_budget)}</Text>
        : <Text color="secondary">—</Text> },
    { key: "effective_admin", header: t("Rôle"), renderCell: (u) => (
        <HStack gap={1} vAlign="center">
          {u.effective_admin ? <Badge label={t("Admin")} variant="warning" /> : <Text color="secondary">{t("Utilisateur")}</Text>}
          {u.managed && (u.role_source === "sso" || u.role_source === "ldap") ? (
            <Text type="supporting" color="secondary">{t("via")} {u.role_source.toUpperCase()}</Text>
          ) : null}
        </HStack>
      ) },
    { key: "enabled", header: t("Statut"), renderCell: (u) => {
        // A blocked account is refused at login whatever its source:
        // state takes precedence over enabled/disabled. `locked_minutes` is the
        // temporary lock after too many login failures — a mere hint.
        const lockedHint = u.locked_minutes > 0
          ? t("verrouillé après trop d'échecs, {n} min").replace("{n}", String(u.locked_minutes))
          : null;
        // LDAP/SSO accounts: the state the DIRECTORY gives (active, disabled
        // there, or gone) — the portal owns no « enabled » flag for them.
        const directory = !u.managed && u.directory_status ? DIRECTORY_STATUS_META[u.directory_status] : null;
        if (!u.blocked && !lockedHint && !u.managed && !directory) return <Text color="secondary">—</Text>;
        // A dot and a word, not a filled pill on every row: the normal state
        // stays quiet, only the abnormal ones draw the eye.
        const state: { label: string; variant: "success" | "error" | "warning" } | null =
          u.blocked ? { label: "Bloqué", variant: "error" }
          : u.managed ? (u.enabled ? { label: "Actif", variant: "success" } : { label: "Désactivé", variant: "error" })
          : directory;
        return (
          <VStack gap={0}>
            {state ? (
              <HStack gap={2} vAlign="center">
                <StatusDot variant={state.variant} label={t(state.label)} />
                <Text color={state.variant === "success" ? "secondary" : undefined}
                  weight={state.variant === "success" ? undefined : "semibold"}>{t(state.label)}</Text>
              </HStack>
            ) : null}
            {u.blocked && u.block_reason ? (
              <Text type="supporting" color="secondary" maxLines={1}>{u.block_reason}</Text>
            ) : null}
            {lockedHint ? <Text type="supporting" color="secondary">{lockedHint}</Text> : null}
          </VStack>
        );
      } },
    { key: "id", header: "", renderCell: (u) => {
        const blocked = u.blocked;
        const items: DropdownMenuOption[] = [
          { label: t("Détails"), icon: EyeIcon, onClick: () => openDetail(u) },
          ...(u.managed ? [{
            label: u.enabled ? t("Désactiver") : t("Activer"),
            icon: u.enabled ? NoSymbolIcon : CheckCircleIcon,
            onClick: () => act(`/admin/users/update/${u.id}`, { enabled: u.enabled ? "0" : "1" }),
          }] : []),
          ...(u.managed ? [{
            label: u.is_admin ? t("Retirer admin") : t("Rendre admin"),
            icon: ShieldCheckIcon,
            onClick: () => act(`/admin/users/update/${u.id}`, { is_admin: u.is_admin ? "0" : "1" }),
          }] : []),
          ...(u.managed ? [{
            label: t("Réinitialiser le mot de passe"), icon: KeyIcon,
            onClick: () => { setFormWarning(null); setPwUser(u); setPw(""); },
          }] : []),
          { label: blocked ? t("Débloquer") : t("Bloquer…"),
            icon: blocked ? CheckCircleIcon : NoSymbolIcon,
            onClick: () => {
              if (blocked) unblockUser(u);
              else { setBlockUser(u); setBlockReason(""); }
            } },
          // Separator before the destructive area — only if it precedes it.
          // Supprimer (deprovision): local accounts only. Purger:
          // directory accounts only (the backend refuses the other way around).
          ...(u.managed && u.id != null ? [
            { type: "divider" as const },
            { label: t("Supprimer"), icon: TrashIcon, onClick: () => openDelete(u) },
          ] : !u.managed ? [
            { type: "divider" as const },
            { label: t("Purger les données…"), icon: ArchiveBoxXMarkIcon, onClick: () => openPurge(u) },
          ] : []),
        ];
        return <MoreMenu label={t("Actions")} size="sm" items={items} />;
      } },
  ];

  return (
    <VStack gap={4}>
      <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
        <Text type="supporting" color="secondary">
          {t("Comptes locaux gérés ici (mots de passe hachés). Le quota vient de la surcharge de l'utilisateur, sinon du groupe, sinon du défaut global.")}
        </Text>
        <HStack gap={2}>
          <Button label={t("Nouveau groupe")} variant="secondary" size="sm"
            icon={<Icon icon={PlusIcon} size="sm" />} onClick={() => {
              setFormWarning(null);
              setNg({ name: "", max_budget: "", is_admin: "0" });
              setEditedGroup(false);
              setGroupDialog(true);
            }} />
          <Button label={t("Nouvel utilisateur")} variant="primary" size="sm"
            icon={<Icon icon={UserPlusIcon} size="sm" />} onClick={() => { setFormWarning(null); setUserDialog(true); }} />
        </HStack>
      </HStack>

      {/* Overview */}
      <Grid columns={{ minWidth: 150, max: 5 }} gap={3}>
        <Tile icon={UsersIcon} value={stats.total} label={t("Comptes connus")} locale={numLocale} />
        <Tile icon={CheckCircleIcon} value={stats.local} label={t("Comptes locaux")} locale={numLocale} />
        <Tile icon={UsersIcon} value={stats.ldap} label="LDAP" locale={numLocale} />
        <Tile icon={UsersIcon} value={stats.sso} label="SSO" locale={numLocale} />
        <Tile icon={ShieldCheckIcon} value={stats.admins} label={t("Administrateurs")} locale={numLocale} />
      </Grid>

      {/* Toolbar */}
      <HStack gap={2} wrap="wrap" vAlign="end">
        <StackItem size="fill">
          <TextInput label={t("Rechercher")} value={query}
            onChange={(v) => { setQuery(v); setPage(1); }}
            placeholder={t("Identifiant ou nom…")} />
        </StackItem>
        <Selector label={t("Source")} value={sourceFilter}
          onChange={(v) => { setSourceFilter(v ?? "all"); setPage(1); }} options={sourceOptions} />
      </HStack>

      {/* Users table */}
      <Card padding={0}>
        {!data ? (
          <HStack padding={4} hAlign="center">
            <HStack width={200}>
              <ProgressBar label={t("Chargement des utilisateurs")} isIndeterminate isLabelHidden />
            </HStack>
          </HStack>
        ) : filtered.length === 0 ? (
          <EmptyState icon={<Icon icon={UsersIcon} size="lg" />} title={t("Aucun utilisateur")}
            description={t("Aucun compte ne correspond à la recherche.")} isCompact />
        ) : (
          <>
            <Table<LocalUser & Record<string, unknown>>
              data={pageUsers as (LocalUser & Record<string, unknown>)[]}
              columns={columns} idKey="id" density="balanced" dividers="rows" />
            <HStack hAlign="center" padding={2}>
              <Pagination
                page={safePage}
                onChange={setPage}
                totalItems={filtered.length}
                pageSize={PAGE_SIZE}
                variant="count"
                size="sm"
              />
            </HStack>
          </>
        )}
      </Card>

      {/* Groups */}
      <Card>
        <VStack gap={3}>
          <Text weight="semibold">{t("Groupes")}</Text>
          {groups.length > 0 ? (
            <VStack gap={1}>
              {groups.map((g) => (
                <HStack key={g.name} hAlign="between" vAlign="center">
                  <HStack gap={2} vAlign="center" wrap="wrap">
                    <Text weight="semibold">{g.name}</Text>
                    {(g.sources ?? []).map((src) => (
                      <Badge key={src} label={SOURCE_META[src]?.label ?? src}
                        variant={SOURCE_META[src]?.variant ?? "neutral"} />
                    ))}
                    <Text type="supporting" color="secondary">
                      {g.max_budget != null ? `${fmtBudget(g.max_budget)} ${period}` : t("quota par défaut")}
                      {g.is_admin ? ` · ${t("admin")}` : ""}
                      {` · ${t("{n} membre(s)").replace("{n}", String(g.members ?? 0))}`}
                    </Text>
                  </HStack>
                  <HStack gap={1}>
                    <Button label={t("Modifier")} variant="ghost" size="sm" isIconOnly
                      icon={<Icon icon={PencilSquareIcon} size="sm" />}
                      onClick={() => {
                        setNg({ name: g.name, max_budget: g.max_budget != null ? String(g.max_budget) : "",
                                is_admin: g.is_admin ? "1" : "0" });
                        setEditedGroup(true);
                        setGroupDialog(true);
                      }} />
                    <Button label={t("Supprimer")} variant="ghost" size="sm" isIconOnly
                      icon={<Icon icon={TrashIcon} size="sm" />}
                      onClick={() => {
                        // A group the directory brings comes back at its
                        // members' next login, without quota or right.
                        const directory = (g.sources ?? []).some((x) => x === "ldap" || x === "sso");
                        const msg = directory
                          ? t("Supprimer ce groupe ? Il vient de l'annuaire : il reviendra à la prochaine connexion d'un membre, sans quota ni droit.")
                          : t("Supprimer ce groupe ?");
                        if (window.confirm(msg)) act(`/admin/groups/delete/${encodeURIComponent(g.name)}`, {});
                      }} />
                  </HStack>
                </HStack>
              ))}
            </VStack>
          ) : (
            <Text type="supporting" color="secondary">{t("Aucun groupe pour l'instant.")}</Text>
          )}
        </VStack>
      </Card>

      <UserCreateDialog
        userDialog={userDialog}
        setUserDialog={setUserDialog}
        setFormWarning={setFormWarning}
        formWarning={formWarning}
        nu={nu}
        setNu={setNu}
        groupOptions={groupOptions}
        data={data}
        fmtBudget={fmtBudget}
        createUser={createUser}
      />

      <GroupCreateDialog
        groupDialog={groupDialog}
        setGroupDialog={setGroupDialog}
        setFormWarning={setFormWarning}
        formWarning={formWarning}
        ng={ng}
        setNg={setNg}
        createGroup={createGroup}
        editing={editedGroup}
      />

      <PasswordResetDialog
        pwUser={pwUser}
        setPwUser={setPwUser}
        setFormWarning={setFormWarning}
        formWarning={formWarning}
        pw={pw}
        setPw={setPw}
        submitPassword={submitPassword}
      />

      <BlockAccountDialog
        blockUser={blockUser}
        setBlockUser={setBlockUser}
        blockReason={blockReason}
        setBlockReason={setBlockReason}
        submitBlock={submitBlock}
        blockBusy={blockBusy}
      />

      <DeleteAccountDialog
        delUser={delUser}
        setDelUser={setDelUser}
        delWarning={delWarning}
        setDelWarning={setDelWarning}
        delConfirm={delConfirm}
        setDelConfirm={setDelConfirm}
        delError={delError}
        submitDelete={submitDelete}
        delBusy={delBusy}
      />

      <PurgeAccountDialog
        purgeUser={purgeUser}
        setPurgeUser={setPurgeUser}
        purgeConfirm={purgeConfirm}
        setPurgeConfirm={setPurgeConfirm}
        purgeError={purgeError}
        submitPurge={submitPurge}
        purgeBusy={purgeBusy}
      />

      <UserDetailDialog
        detailUser={detailUser}
        setDetailUser={setDetailUser}
        detail={detail}
        detailLoading={detailLoading}
        detailError={detailError}
        detailSources={detailSources}
        fmtBudget={fmtBudget}
        sessRevoking={sessRevoking}
        revokeDetailSessions={revokeDetailSessions}
      />
    </VStack>
  );
}
