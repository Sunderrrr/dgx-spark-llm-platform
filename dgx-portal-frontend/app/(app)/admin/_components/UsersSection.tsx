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
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
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
} from "@heroicons/react/24/outline";
import { getJSON, postFormJSON } from "@/lib/api";
import { useT, useLang } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";

type LocalUser = {
  username: string;
  fullname: string | null;
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
};
type Group = { name: string; max_budget: number | null; is_admin: number };
type UsersData = { users: LocalUser[]; groups: Group[]; default_budget: number };

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
  const { lang } = useLang();
  const isNarrow = useIsNarrow();
  const showToast = useToast();
  const numLocale = lang === "fr" ? "fr-FR" : "en-US";
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

  const refresh = useCallback(() => {
    getJSON<UsersData>("/api/admin/users").then(setData).catch(() => {});
  }, []);
  useEffect(refresh, [refresh]);

  const act = useCallback(
    async (url: string, params: Record<string, string>) => {
      if (!csrf) return false;
      try {
        const res = await postFormJSON<{ ok?: boolean; error?: string }>(url, csrf, params);
        if (res && res.ok === false) {
          showToast({ body: res.error ? t(res.error) : t("Échec de l'action."), type: "error" });
          return false;
        }
      } catch {
        showToast({ body: t("Échec de l'action."), type: "error" });
        return false;
      }
      showToast({ body: t("Action effectuée."), type: "info" });
      refresh();
      return true;
    },
    [csrf, refresh, showToast, t],
  );

  const fmtBudget = (n: number) => `${Math.round(n).toLocaleString(numLocale)}`;

  const users = useMemo(() => data?.users ?? [], [data]);
  const groups = data?.groups ?? [];

  // Overview counters for the stat tiles.
  const stats = useMemo(() => ({
    total: users.length,
    local: users.filter((u) => u.managed).length,
    ldap: users.filter((u) => u.sources.includes("ldap")).length,
    sso: users.filter((u) => u.sources.includes("sso")).length,
    admins: users.filter((u) => u.effective_admin).length,
  }), [users]);

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
    if (await act("/admin/users/create", { ...nu })) {
      setNu({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
      setUserDialog(false);
    }
  }
  async function createGroup() {
    if (await act("/admin/groups/create", { ...ng })) {
      setNg({ name: "", max_budget: "", is_admin: "0" });
      setGroupDialog(false);
    }
  }
  async function submitPassword() {
    if (pwUser && pw.length >= 8 && (await act(`/admin/users/update/${pwUser.id}`, { password: pw }))) {
      setPwUser(null);
      setPw("");
    }
  }

  const columns: TableColumn<LocalUser & Record<string, unknown>>[] = [
    { key: "username", header: t("Utilisateur"), renderCell: (u) => (
        <HStack gap={2} vAlign="center">
          <Avatar name={u.fullname || u.username} size="sm" />
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
    { key: "enabled", header: t("Statut"), renderCell: (u) =>
        !u.managed ? <Text color="secondary">—</Text>
        : u.enabled ? <Badge label={t("Actif")} variant="success" /> : <Badge label={t("Désactivé")} variant="error" /> },
    { key: "id", header: "", renderCell: (u) =>
        !u.managed ? <Text color="secondary">—</Text> : (
        <MoreMenu
          label={t("Actions")}
          size="sm"
          items={[
            { label: u.enabled ? t("Désactiver") : t("Activer"), icon: u.enabled ? NoSymbolIcon : CheckCircleIcon,
              onClick: () => act(`/admin/users/update/${u.id}`, { enabled: u.enabled ? "0" : "1" }) },
            { label: u.is_admin ? t("Retirer admin") : t("Rendre admin"), icon: ShieldCheckIcon,
              onClick: () => act(`/admin/users/update/${u.id}`, { is_admin: u.is_admin ? "0" : "1" }) },
            { label: t("Réinitialiser le mot de passe"), icon: KeyIcon, onClick: () => { setPwUser(u); setPw(""); } },
            { type: "divider" as const },
            { label: t("Supprimer"), icon: TrashIcon,
              onClick: () => { if (window.confirm(t("Supprimer cet utilisateur ?"))) act(`/admin/users/delete/${u.id}`, {}); } },
          ]}
        />
      ) },
  ];

  return (
    <VStack gap={4}>
      <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
        <Text type="supporting" color="secondary">
          {t("Comptes locaux gérés ici (mots de passe hachés). Le quota vient de la surcharge de l'utilisateur, sinon du groupe, sinon du défaut global.")}
        </Text>
        <HStack gap={2}>
          <Button label={t("Nouveau groupe")} variant="secondary" size="sm"
            icon={<Icon icon={PlusIcon} size="sm" />} onClick={() => setGroupDialog(true)} />
          <Button label={t("Nouvel utilisateur")} variant="primary" size="sm"
            icon={<Icon icon={UserPlusIcon} size="sm" />} onClick={() => setUserDialog(true)} />
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
                      {g.max_budget != null ? `${fmtBudget(g.max_budget)} / j` : t("quota par défaut")}
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
      <Dialog isOpen={userDialog} onOpenChange={setUserDialog} purpose="form" width={isNarrow ? "94vw" : 520}>
        <Layout
          header={<DialogHeader title={t("Nouvel utilisateur")} hasDivider onOpenChange={setUserDialog} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
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
                <Button label={t("Annuler")} variant="ghost" onClick={() => setUserDialog(false)} />
                <Button label={t("Créer")} variant="primary" onClick={createUser} isDisabled={!nu.username || nu.password.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* New group dialog */}
      <Dialog isOpen={groupDialog} onOpenChange={setGroupDialog} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Nouveau groupe")} hasDivider onOpenChange={setGroupDialog} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
                <TextInput label={t("Nom du groupe")} value={ng.name} onChange={(v) => setNg({ ...ng, name: v })} placeholder="équipe-data" />
                <TextInput label={t("Quota / j (optionnel)")} value={ng.max_budget} onChange={(v) => setNg({ ...ng, max_budget: v })} />
                <Selector label={t("Admin par défaut")} value={ng.is_admin} onChange={(v) => setNg({ ...ng, is_admin: v ?? "0" })} options={BOOL_OPTS(t)} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setGroupDialog(false)} />
                <Button label={t("Ajouter le groupe")} variant="primary" onClick={createGroup} isDisabled={!ng.name} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>

      {/* Reset password dialog */}
      <Dialog isOpen={pwUser != null} onOpenChange={(o) => { if (!o) setPwUser(null); }} purpose="form" width={isNarrow ? "94vw" : 440}>
        <Layout
          header={<DialogHeader title={t("Réinitialiser le mot de passe")} subtitle={pwUser?.username} hasDivider onOpenChange={(o) => { if (!o) setPwUser(null); }} />}
          content={
            <LayoutContent padding={4}>
              <TextInput label={t("Nouveau mot de passe (8 caractères min.)")} type="password" value={pw} onChange={setPw} />
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setPwUser(null)} />
                <Button label={t("Enregistrer")} variant="primary" onClick={submitPassword} isDisabled={pw.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </VStack>
  );
}
