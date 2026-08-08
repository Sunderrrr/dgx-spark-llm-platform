"use client";

import { useCallback, useEffect, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { Heading } from "@astryxdesign/core/Heading";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { useToast } from "@astryxdesign/core/Toast";
import { getJSON, postFormJSON } from "@/lib/api";
import { useT, useLang } from "@/lib/i18n";

type LocalUser = {
  id: number;
  username: string;
  fullname: string | null;
  is_admin: number;
  group_name: string | null;
  max_budget: number | null;
  enabled: number;
  effective_budget: number;
  effective_admin: boolean;
};
type Group = { name: string; max_budget: number | null; is_admin: number };
type UsersData = { users: LocalUser[]; groups: Group[]; default_budget: number };

const BOOL_OPTS = (t: (s: string) => string) => [
  { label: t("Non"), value: "0" },
  { label: t("Oui"), value: "1" },
];

export function UsersSection({ csrf }: { csrf: string }) {
  const t = useT();
  const { lang } = useLang();
  const showToast = useToast();
  const numLocale = lang === "fr" ? "fr-FR" : "en-US";
  const [data, setData] = useState<UsersData | null>(null);
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
          showToast({ body: res.error || t("Échec de l'action."), type: "error" });
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

  const groupOptions = [
    { label: t("Aucun groupe"), value: "" },
    ...(data?.groups ?? []).map((g) => ({ label: g.name, value: g.name })),
  ];

  async function createUser() {
    if (await act("/admin/users/create", { ...nu }))
      setNu({ username: "", password: "", fullname: "", group: "", max_budget: "", is_admin: "0" });
  }
  async function createGroup() {
    if (await act("/admin/groups/create", { ...ng })) setNg({ name: "", max_budget: "", is_admin: "0" });
  }

  const fmtBudget = (n: number) => `${Math.round(n).toLocaleString(numLocale)}`;

  const columns: TableColumn<LocalUser & Record<string, unknown>>[] = [
    { key: "username", header: t("Identifiant"), renderCell: (u) => (
        <VStack gap={0}>
          <Text weight="semibold">{u.username}</Text>
          {u.fullname ? <Text type="supporting" color="secondary">{u.fullname}</Text> : null}
        </VStack>
      ) },
    { key: "group_name", header: t("Groupe"), renderCell: (u) => u.group_name || "—" },
    { key: "effective_budget", header: t("Quota / j"), renderCell: (u) => (
        <Text hasTabularNumbers>{fmtBudget(u.effective_budget)}{u.max_budget == null ? ` (${t("hérité")})` : ""}</Text>
      ) },
    { key: "effective_admin", header: t("Admin"), renderCell: (u) =>
        u.effective_admin ? <Badge label={t("Admin")} variant="warning" /> : <Text color="secondary">—</Text> },
    { key: "enabled", header: t("Statut"), renderCell: (u) =>
        u.enabled ? <Badge label={t("Actif")} variant="success" /> : <Badge label={t("Désactivé")} variant="neutral" /> },
    { key: "id", header: t("Actions"), renderCell: (u) => (
        <HStack gap={1}>
          <Button label={u.enabled ? t("Désactiver") : t("Activer")} variant="ghost" size="sm"
            onClick={() => act(`/admin/users/update/${u.id}`, { enabled: u.enabled ? "0" : "1" })} />
          <Button label={u.is_admin ? t("Retirer admin") : t("Rendre admin")} variant="ghost" size="sm"
            onClick={() => act(`/admin/users/update/${u.id}`, { is_admin: u.is_admin ? "0" : "1" })} />
          <Button label={t("Nouveau mot de passe")} variant="ghost" size="sm"
            onClick={() => {
              const p = window.prompt(t("Nouveau mot de passe (8 caractères min.) :"));
              if (p) act(`/admin/users/update/${u.id}`, { password: p });
            }} />
          <Button label={t("Supprimer")} variant="ghost" size="sm"
            onClick={() => { if (window.confirm(t("Supprimer cet utilisateur ?"))) act(`/admin/users/delete/${u.id}`, {}); }} />
        </HStack>
      ) },
  ];

  return (
    <VStack gap={4}>
      <VStack gap={1}>
        <Heading level={2}>{t("Utilisateurs")}</Heading>
        <Text type="supporting" color="secondary">
          {t("Comptes locaux gérés ici (mots de passe hachés). Le quota vient de la surcharge de l'utilisateur, sinon du groupe, sinon du défaut global.")}
        </Text>
      </VStack>

      <Card>
        <VStack gap={3}>
          <Text weight="semibold">{t("Créer un utilisateur")}</Text>
          <HStack gap={2} wrap="wrap">
            <TextInput label={t("Identifiant")} value={nu.username} onChange={(v) => setNu({ ...nu, username: v })} placeholder="jdupont" />
            <TextInput label={t("Nom complet")} value={nu.fullname} onChange={(v) => setNu({ ...nu, fullname: v })} placeholder="Jean Dupont" />
            <TextInput label={t("Mot de passe")} type="password" value={nu.password} onChange={(v) => setNu({ ...nu, password: v })} />
          </HStack>
          <HStack gap={2} wrap="wrap" vAlign="end">
            <Selector label={t("Groupe")} value={nu.group} onChange={(v) => setNu({ ...nu, group: v })} options={groupOptions} />
            <TextInput label={t("Quota (vide = groupe/défaut)")} value={nu.max_budget} onChange={(v) => setNu({ ...nu, max_budget: v })} placeholder={data ? fmtBudget(data.default_budget) : ""} />
            <Selector label={t("Admin")} value={nu.is_admin} onChange={(v) => setNu({ ...nu, is_admin: v })} options={BOOL_OPTS(t)} />
            <Button label={t("Créer")} variant="primary" onClick={createUser} isDisabled={!nu.username || !nu.password} />
          </HStack>
        </VStack>
      </Card>

      <Card padding={0}>
        <Table<LocalUser & Record<string, unknown>> data={(data?.users ?? []) as (LocalUser & Record<string, unknown>)[]} columns={columns} idKey="id" density="balanced" dividers="rows" />
      </Card>

      <Card>
        <VStack gap={3}>
          <Text weight="semibold">{t("Groupes")}</Text>
          {(data?.groups ?? []).length > 0 && (
            <VStack gap={1}>
              {(data?.groups ?? []).map((g) => (
                <HStack key={g.name} hAlign="between" vAlign="center">
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{g.name}</Text>
                    <Text type="supporting" color="secondary">
                      {g.max_budget != null ? `${fmtBudget(g.max_budget)} / j` : t("quota par défaut")}
                      {g.is_admin ? ` · ${t("admin")}` : ""}
                    </Text>
                  </HStack>
                  <Button label={t("Supprimer")} variant="ghost" size="sm"
                    onClick={() => { if (window.confirm(t("Supprimer ce groupe ?"))) act(`/admin/groups/delete/${encodeURIComponent(g.name)}`, {}); }} />
                </HStack>
              ))}
            </VStack>
          )}
          <HStack gap={2} wrap="wrap" vAlign="end">
            <TextInput label={t("Nom du groupe")} value={ng.name} onChange={(v) => setNg({ ...ng, name: v })} placeholder="équipe-data" />
            <TextInput label={t("Quota / j (optionnel)")} value={ng.max_budget} onChange={(v) => setNg({ ...ng, max_budget: v })} />
            <Selector label={t("Admin par défaut")} value={ng.is_admin} onChange={(v) => setNg({ ...ng, is_admin: v })} options={BOOL_OPTS(t)} />
            <Button label={t("Ajouter le groupe")} variant="secondary" onClick={createGroup} isDisabled={!ng.name} />
          </HStack>
        </VStack>
      </Card>
    </VStack>
  );
}
