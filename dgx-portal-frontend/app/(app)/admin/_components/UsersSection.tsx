"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Icon } from "@astryxdesign/core/Icon";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { Pagination } from "@astryxdesign/core/Pagination";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import type { DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { List, ListItem } from "@astryxdesign/core/List";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Banner } from "@astryxdesign/core/Banner";
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
} from "@heroicons/react/24/outline";
import { getJSON, postFormJSON, sendJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { avatarSrc } from "@/lib/avatar-genere";
import { useMouvementReduit } from "@/lib/use-mouvement-reduit";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { SessionsList, type AccountSession } from "../../_components/SessionsList";

type LocalUser = {
  username: string;
  fullname: string | null;
  /** Logo de marque choisi ; `null` = avatar généré depuis le pseudo. */
  avatar_id: string | null;
  sources: string[];
  managed: boolean;
  managed_by: "local" | "repertoire";
  id: number | null;
  group_name: string | null;
  enabled: number;
  is_admin: number | null;
  effective_admin: boolean | null;
  role_source: "local" | "sso" | "ldap" | "externe" | null;
  last_source: string | null;
  effective_budget: number | null;
  unlimited: boolean;
  spend: number;
  key_count: number;
  last_seen: string | null;
  // État de blocage / verrouillage renvoyé par /api/admin/users. Un compte
  // bloqué est refusé au login quelle que soit sa source (LDAP/SSO compris) ;
  // locked_minutes est le verrou temporaire après trop d'échecs de login.
  blocked: boolean;
  block_reason: string | null;
  blocked_at: string | null;
  locked_minutes: number;
};
type Group = { name: string; max_budget: number | null; is_admin: number };
type UsersData = { users: LocalUser[]; groups: Group[]; default_budget: number };

// Réponse de GET /admin/users/<username>/detail. Chaque champ peut manquer
// (compte LDAP/SSO sans ligne locale, utilisateur sans profil LiteLLM…).
type AdminUserDetail = {
  ok?: boolean;
  username?: string | null;
  fullname?: string | null;
  sources?: string | null; // "ldap,sso" — chaîne à virgules, pas un tableau
  last_source?: string | null;
  last_seen?: string | null;
  role?: string | null;
  enabled?: boolean;
  local?: boolean;
  group?: string | null;
  max_budget?: number | null;
  effective_budget?: number | null;
  blocked?: { blocked?: boolean; reason?: string | null; at?: string | null; by?: string | null } | null;
  litellm?: { exists?: boolean; max_budget?: number | null; spend?: number | null; budget_duration?: string | null } | null;
  keys?: { alias?: string | null; created_at?: string | null; spend?: number | null }[] | null;
  memory_facts?: number | null;
  conversations?: number | null;
  sessions?: AccountSession[] | null;
  audit?: { action?: string | null; detail?: string | null; by?: string | null; at?: string | null }[] | null;
};

// Category tags (auth source) → non-semantic color variants.
const SOURCE_META: Record<string, { label: string; variant: "green" | "orange" | "blue" | "purple" | "neutral" }> = {
  local: { label: "Local", variant: "green" },
  ldap: { label: "LDAP", variant: "blue" },
  sso: { label: "SSO", variant: "purple" },
  externe: { label: "Externe", variant: "neutral" },
};

const BOOL_OPTS = (t: (s: string) => string) => [
  { label: t("Non"), value: "0" },
  { label: t("Oui"), value: "1" },
];

// Pagination de la table des utilisateurs (client-side).
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

export function UsersSection({ csrf }: { csrf: string }) {
  const t = useT();
  const isNarrow = useIsNarrow();
  const mouvementReduit = useMouvementReduit();
  const showToast = useToast();
  const numLocale = useLocale();
  const [data, setData] = useState<UsersData | null>(null);

  // Toolbar state: free-text search + auth-source filter.
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  // État de chargement (premier fetch) + page courante de la table.
  const [page, setPage] = useState(1);

  // Dialog state — create forms and the (masked) password reset live in modals
  // instead of always-open forms / window.prompt.
  const [userDialog, setUserDialog] = useState(false);
  const [groupDialog, setGroupDialog] = useState(false);
  const [pwUser, setPwUser] = useState<LocalUser | null>(null);
  const [pw, setPw] = useState("");
  const [nu, setNu] = useState({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
  const [ng, setNg] = useState({ name: "", max_budget: "", is_admin: "0" });
  // Blocage d'un compte (raison demandée, optionnelle).
  const [blockUser, setBlockUser] = useState<LocalUser | null>(null);
  const [blockReason, setBlockReason] = useState("");
  const [blockBusy, setBlockBusy] = useState(false);
  // Suppression : destructive, confirmation en tapant DELETE.
  const [delUser, setDelUser] = useState<LocalUser | null>(null);
  const [delConfirm, setDelConfirm] = useState("");
  const [delBusy, setDelBusy] = useState(false);
  const [delError, setDelError] = useState<string | null>(null);
  // Warning de succès ({ok:true, warning}) : des clés n'ont pas pu être
  // révoquées côté LiteLLM — affiché à côté du succès, pas comme un échec.
  const [delWarning, setDelWarning] = useState<string | null>(null);
  // Purge des données d'un compte d'annuaire (LDAP/SSO, sans ligne locale) :
  // efface ses données SANS retirer l'accès — l'offboarding, c'est Bloquer.
  const [purgeUser, setPurgeUser] = useState<LocalUser | null>(null);
  const [purgeConfirm, setPurgeConfirm] = useState("");
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  // Détail d'un compte (drawer) : chargé à l'ouverture.
  const [detailUser, setDetailUser] = useState<LocalUser | null>(null);
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [sessRevoking, setSessRevoking] = useState(false);
  // Avertissement renvoyé par create/update ({ok:true, warning}) : le quota
  // LiteLLM n'a pas pu s'appliquer, le compte est temporairement sans plafond.
  // Le dialog reste ouvert pour le lire, un Banner l'affiche en haut.
  const [formWarning, setFormWarning] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getJSON<UsersData>("/api/admin/users").then(setData).catch(() => {});
  }, []);
  useEffect(refresh, [refresh]);

  // Renvoie false en cas d'échec, sinon {ok:true, warning?}. `warning`
  // (quota LiteLLM non appliqué…) accompagne un SUCCÈS : il s'affiche bien
  // en évidence dans le dialog, pas comme une erreur.
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

  // Comme act(), mais vers les nouvelles routes JSON ({confirm, reason}…) —
  // les erreurs {ok:false, error} passent par t() comme dans act() : une
  // phrase serveur inconnue retombe sur elle-même (fallback silencieux).
  // Retourne la réponse ou null, à l'appelant de choisir son toast de succès.
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

  // Détail : `sources` est une chaîne « ldap,sso » — on la découpe pour les badges.
  const detailSources = detail?.sources
    ? detail.sources.split(",").map((s) => s.trim()).filter(Boolean)
    : [];

  const users = useMemo(() => data?.users ?? [], [data]);
  const groups = data?.groups ?? [];

  // Overview counters for the stat tiles.
  const stats = useMemo(() => {
    // Un SEUL parcours : quatre `filter` complets sur la même liste, en plus de
    // celui de `filtered`, faisaient cinq lectures pour une seule information.
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
      const srcs = u.sources.length ? u.sources : ["externe"];
      const matchesSrc = sourceFilter === "all" || srcs.includes(sourceFilter);
      return matchesQ && matchesSrc;
    });
  }, [users, query, sourceFilter]);

  // Slicing de la page courante (pagination client-side).
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
    { label: t("Externe"), value: "externe" },
  ];

  async function createUser() {
    const res = await act("/admin/users/create", { ...nu });
    if (!res) return;
    setNu({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
    if (res.warning) setFormWarning(res.warning); // dialog laissé ouvert pour lire le warning
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

  // Détail d'un compte : (re)chargé à l'ouverture et après une action qui le
  // modifie (révocation de sessions) — toujours depuis un handler, pas un
  // effet : l'état « détail » n'a pas à se synchroniser tout seul.
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

  // Purge d'un compte d'annuaire : données effacées, accès conservé. Erreurs
  // {ok:false, error} (400/409) affichées verbatim dans le dialog.
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
    // Raison optionnelle : on ne l'envoie que si l'admin en a saisi une.
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
    // Le corps {"confirm": "DELETE"} est exigé par la route ; une erreur
    // {ok:false, error} s'affiche verbatim dans le dialog.
    const res = await sendJSON<{ ok?: boolean; error?: string; warning?: string }>(
      `/admin/users/delete/${delUser.id}`, csrf, { confirm: "DELETE" });
    setDelBusy(false);
    if (res && res.ok === false) {
      setDelError(res.error || t("Échec de l'action."));
      return;
    }
    showToast({ body: t("Compte supprimé."), type: "info" });
    refresh();
    // Warning de succès (clés non révoquées côté LiteLLM) : le dialog reste
    // ouvert pour l'afficher — jamais un échec de sécurité silencieux.
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
          <Avatar
            src={avatarSrc(u.avatar_id, u.username, !mouvementReduit)}
            name={u.fullname || u.username}
            size="sm"
          />
          <VStack gap={0}>
            <Text weight="semibold">{u.username}</Text>
            {u.fullname ? <Text type="supporting" color="secondary">{u.fullname}</Text> : null}
          </VStack>
        </HStack>
      ) },
    { key: "sources", header: t("Source"), renderCell: (u) => (
        <HStack gap={1} wrap="wrap">
          {(u.sources.length ? u.sources : ["externe"]).map((s) => {
            const m = SOURCE_META[s] ?? { label: s, variant: "neutral" as const };
            return <Badge key={s} label={t(m.label)} variant={m.variant} />;
          })}
        </HStack>
      ) },
    { key: "managed", header: t("Géré"), renderCell: (u) =>
        u.managed_by === "local" ? <Badge label={t("Ici")} variant="green" /> : <Badge label="Authentik" variant="neutral" /> },
    { key: "group_name", header: t("Groupe"), renderCell: (u) => u.group_name || "—" },
    { key: "effective_budget", header: t("Quota / j"), renderCell: (u) =>
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
        // Un compte bloqué est refusé au login quelle que soit sa source :
        // état prioritaire sur actif/désactivé. `locked_minutes` est le
        // verrou temporaire après trop d'échecs de login — simple indice.
        const lockedHint = u.locked_minutes > 0
          ? t("verrouillé après trop d'échecs, {n} min").replace("{n}", String(u.locked_minutes))
          : null;
        if (!u.blocked && !lockedHint && !u.managed) return <Text color="secondary">—</Text>;
        return (
          <VStack gap={0}>
            {u.blocked ? (
              <Badge label={t("Bloqué")} variant="error" />
            ) : u.managed ? (
              u.enabled ? <Badge label={t("Actif")} variant="success" /> : <Badge label={t("Désactivé")} variant="error" />
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
          // Séparateur avant la zone destructive — seulement s'il la précède.
          // Supprimer (déprovisionner) : comptes locaux uniquement. Purger :
          // comptes d'annuaire uniquement (le backend refuse l'inverse).
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
            icon={<Icon icon={PlusIcon} size="sm" />} onClick={() => { setFormWarning(null); setGroupDialog(true); }} />
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
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{g.name}</Text>
                    <Text type="supporting" color="secondary">
                      {g.max_budget != null ? `${fmtBudget(g.max_budget)} ${t("/ j")}` : t("quota par défaut")}
                      {g.is_admin ? ` · ${t("admin")}` : ""}
                    </Text>
                  </HStack>
                  <Button label={t("Supprimer")} variant="ghost" size="sm" isIconOnly
                    icon={<Icon icon={TrashIcon} size="sm" />}
                    onClick={() => { if (window.confirm(t("Supprimer ce groupe ?"))) act(`/admin/groups/delete/${encodeURIComponent(g.name)}`, {}); }} />
                </HStack>
              ))}
            </VStack>
          ) : (
            <Text type="supporting" color="secondary">{t("Aucun groupe pour l'instant.")}</Text>
          )}
        </VStack>
      </Card>

      {/* New user dialog */}
      <Dialog isOpen={userDialog} onOpenChange={(o) => { setUserDialog(o); if (!o) setFormWarning(null); }} purpose="form" width={isNarrow ? "94vw" : 520}>
        <Layout
          header={<DialogHeader title={t("Nouvel utilisateur")} hasDivider onOpenChange={(o) => { setUserDialog(o); if (!o) setFormWarning(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Identifiant")} value={nu.username} onChange={(v) => setNu({ ...nu, username: v })} placeholder="jdupont" />
                <TextInput label={t("Nom complet")} value={nu.fullname} onChange={(v) => setNu({ ...nu, fullname: v })} placeholder="Jean Dupont" />
                <TextInput label={t("Mot de passe")} type="password" value={nu.password} onChange={(v) => setNu({ ...nu, password: v })} />
                <Selector label={t("Groupe")} value={nu.group} onChange={(v) => setNu({ ...nu, group: v ?? "" })} options={groupOptions} />
                <TextInput label={t("Quota (vide = groupe/défaut)")} value={nu.max_budget} onChange={(v) => setNu({ ...nu, max_budget: v })} placeholder={data ? fmtBudget(data.default_budget) : ""} />
                <Selector label={t("Admin")} value={nu.is_admin} onChange={(v) => setNu({ ...nu, is_admin: v ?? "0" })} options={BOOL_OPTS(t)} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setUserDialog(false); }} />
                <Button label={t("Créer")} variant="primary" onClick={createUser} isDisabled={!nu.username || nu.password.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* New group dialog */}
      <Dialog isOpen={groupDialog} onOpenChange={(o) => { setGroupDialog(o); if (!o) setFormWarning(null); }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Nouveau groupe")} hasDivider onOpenChange={(o) => { setGroupDialog(o); if (!o) setFormWarning(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Nom du groupe")} value={ng.name} onChange={(v) => setNg({ ...ng, name: v })} placeholder="équipe-data" />
                <TextInput label={t("Quota / j (optionnel)")} value={ng.max_budget} onChange={(v) => setNg({ ...ng, max_budget: v })} />
                <Selector label={t("Admin par défaut")} value={ng.is_admin} onChange={(v) => setNg({ ...ng, is_admin: v ?? "0" })} options={BOOL_OPTS(t)} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setGroupDialog(false); }} />
                <Button label={t("Ajouter le groupe")} variant="primary" onClick={createGroup} isDisabled={!ng.name} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Reset password dialog */}
      <Dialog isOpen={pwUser != null} onOpenChange={(o) => { if (!o) { setPwUser(null); setFormWarning(null); } }} purpose="form" width={isNarrow ? "94vw" : 440}>
        <Layout
          header={<DialogHeader title={t("Réinitialiser le mot de passe")} subtitle={pwUser?.username} hasDivider onOpenChange={(o) => { if (!o) { setPwUser(null); setFormWarning(null); } }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Nouveau mot de passe (8 caractères min.)")} type="password" value={pw} onChange={setPw} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setPwUser(null); }} />
                <Button label={t("Enregistrer")} variant="primary" onClick={submitPassword} isDisabled={pw.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Block dialog — la raison est optionnelle mais demandée */}
      <Dialog isOpen={blockUser != null} onOpenChange={(o) => { if (!o) setBlockUser(null); }} purpose="form" width={isNarrow ? "94vw" : 440}>
        <Layout
          header={<DialogHeader title={t("Bloquer ce compte")} subtitle={blockUser?.username} hasDivider onOpenChange={(o) => { if (!o) setBlockUser(null); }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Le compte sera refusé au login, quelle que soit la source d'authentification, et ses sessions actives seront révoquées.")}
                </Text>
                <TextInput label={t("Raison (optionnel)")} value={blockReason} onChange={setBlockReason} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setBlockUser(null)} />
                <Button label={t("Bloquer")} variant="destructive" onClick={submitBlock} isLoading={blockBusy} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Delete dialog — destructif : confirmation en tapant DELETE */}
      <Dialog isOpen={delUser != null} onOpenChange={(o) => { if (!o) { setDelUser(null); setDelWarning(null); } }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Supprimer ce compte")} subtitle={delUser?.username} hasDivider onOpenChange={(o) => { if (!o) { setDelUser(null); setDelWarning(null); } }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Cette action est définitive : elle révoque les clés API et les sessions du compte, supprime son enveloppe LiteLLM ET ses données personnelles (mémoires, conversations, préférences, passkeys).")}
                </Text>
                {/* Warning de succès : des clés n'ont pas pu être révoquées
                    côté LiteLLM — à lire, jamais silencieux. */}
                {delWarning && <Banner status="warning" title={delWarning} />}
                <TextInput label={t("Tapez DELETE pour confirmer")} value={delConfirm} onChange={setDelConfirm} />
                {delError && <Banner status="error" title={delError} />}
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setDelUser(null); setDelWarning(null); }} />
                <Button label={t("Supprimer définitivement")} variant="destructive" onClick={submitDelete}
                  isLoading={delBusy} isDisabled={delConfirm !== "DELETE"} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Purge dialog — comptes d'annuaire (LDAP/SSO) : efface les données,
          PAS l'accès ; c'est Bloquer qui fait l'offboarding. */}
      <Dialog isOpen={purgeUser != null} onOpenChange={(o) => { if (!o) setPurgeUser(null); }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Purger les données")} subtitle={purgeUser?.username} hasDivider onOpenChange={(o) => { if (!o) setPurgeUser(null); }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Cette action efface DÉFINITIVEMENT les données du compte : mémoires, conversations, liens de partage, préférences et clés API.")}
                </Text>
                <Text type="supporting" color="secondary">
                  {t("Elle ne retire pas l'accès : un compte LDAP/SSO peut se reconnecter et repartira d'un compte vide. Pour retirer l'accès, utilise Bloquer.")}
                </Text>
                <TextInput label={t("Tapez DELETE pour confirmer")} value={purgeConfirm} onChange={setPurgeConfirm} />
                {purgeError && <Banner status="error" title={purgeError} />}
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setPurgeUser(null)} />
                <Button label={t("Purger")} variant="destructive" onClick={submitPurge}
                  isLoading={purgeBusy} isDisabled={purgeConfirm !== "DELETE"} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Detail drawer — identité, budget, clés, sessions et audit du compte */}
      <Dialog isOpen={detailUser != null} onOpenChange={(o) => { if (!o) setDetailUser(null); }} purpose="form" width={isNarrow ? "94vw" : 640}>
        <Layout
          header={<DialogHeader title={t("Détails du compte")} subtitle={detailUser?.username} hasDivider onOpenChange={(o) => { if (!o) setDetailUser(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              {detailError ? <Banner status="error" title={detailError} /> : null}
              {detailLoading && !detail ? (
                <HStack hAlign="center" padding={4}>
                  <HStack width={200}>
                    <ProgressBar label={t("Chargement…")} isIndeterminate isLabelHidden />
                  </HStack>
                </HStack>
              ) : detail ? (
                <VStack gap={4}>
                  {/* Identité, rôle, sources, dernière activité */}
                  <HStack gap={3} vAlign="center">
                    <Avatar
                      src={avatarSrc(detailUser?.avatar_id, detailUser?.username || detail.username || "", !mouvementReduit)}
                      name={detail.fullname || detail.username || detailUser?.username || "?"}
                      size="md"
                    />
                    <VStack gap={0}>
                      <HStack gap={2} vAlign="center" wrap="wrap">
                        <Text weight="semibold">{detail.username ?? detailUser?.username}</Text>
                        {detail.role === "admin" ? <Badge label={t("Admin")} variant="warning" /> : null}
                        {detail.blocked?.blocked ? <Badge label={t("Bloqué")} variant="error" /> : null}
                        {detail.enabled === false ? <Badge label={t("Désactivé")} variant="error" /> : null}
                      </HStack>
                      {detail.fullname ? <Text type="supporting" color="secondary">{detail.fullname}</Text> : null}
                      <HStack gap={1} vAlign="center" wrap="wrap">
                        {detailSources.map((s) => {
                          const m = SOURCE_META[s] ?? { label: s, variant: "neutral" as const };
                          return <Badge key={s} label={t(m.label)} variant={m.variant} />;
                        })}
                        {detail.last_source ? (
                          <Text type="supporting" color="secondary">
                            {t("Dernière source")} : {detail.last_source.toUpperCase()}
                          </Text>
                        ) : null}
                      </HStack>
                      {detail.last_seen ? (
                        <Text type="supporting" color="secondary">
                          {t("Dernière activité")} : <Timestamp value={detail.last_seen} format="date_time" />
                        </Text>
                      ) : null}
                    </VStack>
                  </HStack>

                  {/* Blocage actif : raison, auteur, date */}
                  {detail.blocked?.blocked ? (
                    <HStack gap={2} vAlign="center" wrap="wrap">
                      <Badge label={t("Bloqué")} variant="error" />
                      {detail.blocked.reason ? <Text type="supporting" color="secondary">{detail.blocked.reason}</Text> : null}
                      {detail.blocked.by ? <Text type="supporting" color="secondary">{t("par")} {detail.blocked.by}</Text> : null}
                      {detail.blocked.at ? <Timestamp value={detail.blocked.at} format="date_time" /> : null}
                    </HStack>
                  ) : null}

                  {/* Budget : surcharge → effectif, et dépense LiteLLM */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Budget")}</Text>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Surcharge utilisateur")}</Text>
                      <Text hasTabularNumbers>{detail.max_budget != null ? fmtBudget(detail.max_budget) : "—"}</Text>
                    </HStack>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Quota effectif")}</Text>
                      <Text hasTabularNumbers>
                        {detail.effective_budget != null ? `${fmtBudget(detail.effective_budget)} ${t(detail.effective_budget > 1 ? "tokens" : "token")}` : "—"}
                      </Text>
                    </HStack>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Dépensé (LiteLLM)")}</Text>
                      <Text hasTabularNumbers>
                        {detail.litellm?.exists ? `${fmtBudget(detail.litellm.spend ?? 0)} ${t((detail.litellm.spend ?? 0) > 1 ? "tokens" : "token")}` : t("Aucun profil LiteLLM")}
                      </Text>
                    </HStack>
                    {detail.litellm?.budget_duration ? (
                      <HStack hAlign="between">
                        <Text type="supporting" color="secondary">{t("Fenêtre de budget")}</Text>
                        <Text>{detail.litellm.budget_duration}</Text>
                      </HStack>
                    ) : null}
                    {detail.group ? (
                      <HStack hAlign="between">
                        <Text type="supporting" color="secondary">{t("Groupe")}</Text>
                        <Text>{detail.group}</Text>
                      </HStack>
                    ) : null}
                  </VStack>

                  {/* Clés — alias, création, dépense. Jamais de valeur de clé :
                      le payload ne peut pas en contenir. */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Clés")}</Text>
                    {(detail.keys ?? []).length === 0 ? (
                      <Text type="supporting" color="secondary">{t("Aucune clé pour l'instant.")}</Text>
                    ) : (
                      <List hasDividers>
                        {(detail.keys ?? []).map((k, i) => (
                          <ListItem
                            key={`${k.alias ?? ""}-${i}`}
                            startContent={<Icon icon={KeyIcon} size="sm" color="secondary" />}
                            label={k.alias || t("Sans nom")}
                            description={k.created_at ? <Timestamp value={k.created_at} format="date_time" /> : undefined}
                            endContent={
                              <Text type="supporting" color="secondary" hasTabularNumbers>
                                {`${Math.round(k.spend ?? 0).toLocaleString(numLocale)} ${t(Math.round(k.spend ?? 0) > 1 ? "tokens" : "token")}`}
                              </Text>
                            }
                          />
                        ))}
                      </List>
                    )}
                  </VStack>

                  {/* Mémoire & conversations */}
                  <HStack gap={4} vAlign="center">
                    <HStack gap={1} vAlign="center">
                      <Text type="supporting" color="secondary">{t("Mémoire")} :</Text>
                      <Text hasTabularNumbers>{detail.memory_facts ?? 0}</Text>
                    </HStack>
                    <HStack gap={1} vAlign="center">
                      <Text type="supporting" color="secondary">{t("Conversations")} :</Text>
                      <Text hasTabularNumbers>{detail.conversations ?? 0}</Text>
                    </HStack>
                  </HStack>

                  {/* Sessions — même présentation que la liste self-service */}
                  <VStack gap={2}>
                    <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                      <Text weight="semibold">{t("Sessions")}</Text>
                      <Button
                        label={t("Révoquer toutes ses sessions")}
                        variant="secondary"
                        size="sm"
                        isLoading={sessRevoking}
                        isDisabled={(detail.sessions ?? []).length === 0}
                        onClick={revokeDetailSessions}
                      />
                    </HStack>
                    <SessionsList sessions={detail.sessions ?? []} />
                  </VStack>

                  {/* Audit récent */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Dernières actions de ce compte")}</Text>
                    {(detail.audit ?? []).length === 0 ? (
                      <Text type="supporting" color="secondary">{t("Aucune entrée")}</Text>
                    ) : (
                      <List hasDividers>
                        {(detail.audit ?? []).map((a, i) => (
                          <ListItem
                            key={`${a.action ?? ""}-${i}`}
                            label={a.action || "—"}
                            description={
                              <VStack gap={0}>
                                {a.detail ? <Text type="supporting" color="secondary">{a.detail}</Text> : null}
                                <HStack gap={1} vAlign="center">
                                  {a.by ? <Text type="supporting" color="secondary">{a.by}</Text> : null}
                                  {a.at ? <Timestamp value={a.at} format="date_time" /> : null}
                                </HStack>
                              </VStack>
                            }
                          />
                        ))}
                      </List>
                    )}
                  </VStack>
                </VStack>
              ) : null}
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Fermer")} variant="ghost" onClick={() => setDetailUser(null)} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </VStack>
  );
}
