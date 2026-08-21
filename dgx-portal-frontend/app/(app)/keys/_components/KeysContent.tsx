"use client";

import { useEffect, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { TabList, Tab } from "@astryxdesign/core/TabList";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { useToast } from "@astryxdesign/core/Toast";
import { PlusIcon, TrashIcon, EyeIcon, EyeSlashIcon, KeyIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, postForm } from "@/lib/api";
import {
  INTEGRATION_TOOLS,
  buildSnippet,
  snippetLanguage,
  type IntegrationTool,
  type ModelLimit,
} from "@/lib/integrationSnippets";
import { useT, useLang } from "@/lib/i18n";

type DiscordStatus = { linkable: boolean; dm_enabled: boolean; linked: boolean; discord_name: string };
type ApiKey = { key_alias: string; key: string; created_at: string; spend: number };
type Account = { spend: number; max_budget: number; budget_reset_at: string | null; unlimited: boolean; has_pending: boolean };
type KeysData = {
  user_keys: ApiKey[];
  budget_tokens: string;
  budget_duration: string;
  account: Account;
  model_limits: Record<string, ModelLimit>;
  running_models: string[];
  auto_model: string;
  public_api_url: string;
};

export function KeysContent() {
  const t = useT();
  const { lang } = useLang();
  // The thousands separator follows the UI language (space in French,
  // comma in English) instead of being hardcoded to fr-FR.
  const numLocale = lang === "fr" ? "fr-FR" : "en-US";
  const csrf = useCsrf();
  const showToast = useToast();
  const [data, setData] = useState<KeysData | null>(null);
  const [keyName, setKeyName] = useState("");
  const [budgetReason, setBudgetReason] = useState("");
  const [showBudgetForm, setShowBudgetForm] = useState(false);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [revealKeyInSnippet, setRevealKeyInSnippet] = useState(false);
  const [selectedKey, setSelectedKey] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [tool, setTool] = useState<IntegrationTool>("opencode");
  const [discord, setDiscord] = useState<DiscordStatus | null>(null);

  function refresh() {
    getJSON<KeysData>("/api/keys").then((d) => {
      setData(d);
      if (!selectedKey && d.user_keys.length) setSelectedKey(d.user_keys[0].key);
      // Default: the virtual "auto-model" (follows the currently-running model).
      if (!selectedModel) setSelectedModel(d.auto_model || d.running_models[0] || "");
    });
    getJSON<DiscordStatus>("/api/discord/status").then(setDiscord).catch(() => {});
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps -- should only run on mount
  useEffect(refresh, []);

  async function createKey() {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "create", key_name: keyName });
    setKeyName("");
    showToast({ body: t("Clé créée !"), type: "info" });
    refresh();
  }

  async function revokeKey(key: string) {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "revoke", key });
    showToast({ body: t("Clé révoquée."), type: "info" });
    refresh();
  }

  async function requestBudget() {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "request_budget", reason: budgetReason });
    setBudgetReason("");
    setShowBudgetForm(false);
    showToast({ body: t("Demande de tokens envoyée !"), type: "info" });
    refresh();
  }

  async function unlinkDiscord() {
    if (!csrf) return;
    await postForm("/discord/unlink", csrf, {});
    showToast({ body: t("Compte Discord délié."), type: "info" });
    refresh();
  }

  const columns: TableColumn<ApiKey & Record<string, unknown>>[] = [
    { key: "key_alias", header: t("Alias") },
    {
      key: "key",
      header: t("Clé"),
      renderCell: (row) => (
        <HStack gap={2} vAlign="center">
          <Text type="code" hasTabularNumbers>
            {revealed.has(row.key) ? row.key : `${row.key.slice(0, 10)}…${row.key.slice(-4)}`}
          </Text>
          <Button
            label={t("Afficher")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={revealed.has(row.key) ? EyeSlashIcon : EyeIcon} size="sm" />}
            onClick={() =>
              setRevealed((prev) => {
                const next = new Set(prev);
                if (next.has(row.key)) next.delete(row.key);
                else next.add(row.key);
                return next;
              })
            }
          />
        </HStack>
      ),
    },
    { key: "spend", header: t("Dépensé"), renderCell: (row) => `${Math.round(row.spend || 0).toLocaleString("fr-FR")} tokens` },
    {
      key: "actions" as keyof ApiKey,
      header: "",
      renderCell: (row) => (
        <Button label={t("Révoquer")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => revokeKey(row.key)} />
      ),
    },
  ];

  const pct = data && !data.account.unlimited && data.account.max_budget ? (data.account.spend / data.account.max_budget) * 100 : 0;

  return (
    <VStack gap={5} maxWidth={980}>
      <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
        <VStack gap={1}>
          <Heading level={1}>{t("Mes clés API")}</Heading>
          <Text type="supporting" color="secondary">{t("Des clés personnelles pour appeler les modèles via l'API compatible OpenAI.")}</Text>
        </VStack>
        <HStack gap={2} vAlign="end">
          <TextInput
            label={t("Nom")}
            isLabelHidden
            value={keyName}
            onChange={setKeyName}
            placeholder={t("Nom (ex: mon-laptop)")}
            size="sm"
          />
          <Button label={t("Nouvelle clé")} variant="primary" size="sm" icon={<Icon icon={PlusIcon} size="sm" />} onClick={createKey} />
        </HStack>
      </HStack>

      {data && (
        <Card>
          <VStack gap={2}>
            <Text type="supporting" color="secondary">
              Endpoint : {data.public_api_url} — compatible OpenAI.
            </Text>
            {data.account.unlimited ? (
              <HStack>
                <Badge label={t("Budget illimité (admin)")} variant="warning" />
              </HStack>
            ) : (
              <VStack gap={1}>
                <HStack hAlign="between">
                  <Text type="supporting" color="secondary">
                    {t("Budget du compte — partagé par toutes tes clés /")} {data.budget_duration}
                  </Text>
                  <Text type="supporting" color="secondary">
                    {Math.round(data.account.spend).toLocaleString(numLocale)} / {Math.round(data.account.max_budget).toLocaleString(numLocale)} tokens
                  </Text>
                </HStack>
                <ProgressBar
                  label={t("Budget")}
                  isLabelHidden
                  value={Math.min(pct, 100)}
                  variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                />
                <HStack gap={3} vAlign="center">
                  <Text type="supporting" color="secondary">
                    {Math.max(Math.round(data.account.max_budget - data.account.spend), 0).toLocaleString(numLocale)} {t("tokens restants")}
                  </Text>
                  {data.account.has_pending ? (
                    <Badge label={t("Demande en attente")} variant="neutral" />
                  ) : (
                    <Button
                      label={t("Demander plus de tokens")}
                      variant="secondary"
                      size="sm"
                      onClick={() => setShowBudgetForm((v) => !v)}
                    />
                  )}
                </HStack>
                {showBudgetForm && (
                  <HStack gap={2}>
                    <TextInput label={t("Raison")} isLabelHidden value={budgetReason} onChange={setBudgetReason} placeholder={t("Raison (optionnel)")} size="sm" />
                    <Button label={t("Envoyer")} variant="secondary" size="sm" onClick={requestBudget} />
                  </HStack>
                )}
              </VStack>
            )}
          </VStack>
        </Card>
      )}

      {discord && (discord.linkable || discord.linked) && (
        <Card>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={1}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{t("Notifications Discord")}</Text>
                {discord.linked ? <Badge label={t("Lié")} variant="success" /> : null}
              </HStack>
              <Text type="supporting" color="secondary">
                {discord.linked
                  ? `${t("Compte Discord lié :")} ${discord.discord_name || "—"}`
                  : t("Lie ton compte Discord pour recevoir les annonces (changement de modèle, maintenance…) en message privé.")}
              </Text>
            </VStack>
            {discord.linked ? (
              <Button label={t("Délier")} variant="secondary" size="sm" onClick={unlinkDiscord} />
            ) : (
              <Button
                label={t("Lier mon compte Discord")}
                variant="primary"
                size="sm"
                onClick={() => { window.location.href = "/discord/link"; }}
              />
            )}
          </HStack>
        </Card>
      )}

      {data && data.user_keys.length === 0 && (
        <EmptyState
          icon={<Icon icon={KeyIcon} size="lg" />}
          title={t("Aucune clé pour l'instant.")}
          description={t("Utilise « Nouvelle clé » en haut à droite pour en générer une.")}
        />
      )}

      {data && data.user_keys.length > 0 && (
        <Card padding={0}>
          <Table<ApiKey & Record<string, unknown>> data={data.user_keys} columns={columns} idKey="key" density="balanced" dividers="rows" />
        </Card>
      )}

      {data && data.user_keys.length > 0 && (
        <Card>
          <VStack gap={3}>
            <Text weight="semibold">{t("Intégrations")}</Text>
            <HStack gap={3} wrap="wrap">
              <Selector label={t("Clé")} value={selectedKey} onChange={(v) => setSelectedKey(v ?? "")} options={data.user_keys.map((k) => ({ value: k.key, label: k.key_alias }))} />
              <Selector
                label={t("Modèle")}
                value={selectedModel}
                onChange={(v) => setSelectedModel(v ?? "")}
                options={[
                  ...(data.auto_model ? [{ value: data.auto_model, label: `${data.auto_model} — ${t("recommandé")}` }] : []),
                  ...data.running_models.filter((m) => m !== data.auto_model).map((m) => ({ value: m, label: m })),
                ]}
              />
            </HStack>
            {selectedModel === data.auto_model && (
              <Text type="supporting" color="secondary">
                {t("Modèle virtuel : route toujours vers le modèle chat actuellement chargé — ton code n'a rien à changer quand l'admin bascule de modèle. Choisis un modèle nommé pour t'épingler à celui-là.")}
              </Text>
            )}
            {/* La bande d'onglets dépasse la largeur du panneau (une douzaine
                d'intégrations pour ~684 px) : sans conteneur défilant, les
                derniers onglets débordent hors du cadre et sont inatteignables. */}
            <HStack width="100%" style={{ overflowX: "auto" }}>
              <TabList value={tool} onChange={(v) => setTool(v as IntegrationTool)}>
                {INTEGRATION_TOOLS.map((t) => (
                  <Tab key={t.value} value={t.value} label={t.label} />
                ))}
              </TabList>
            </HStack>
            <Button
              label={revealKeyInSnippet ? t("Masquer la clé") : t("Révéler la clé")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={revealKeyInSnippet ? EyeSlashIcon : EyeIcon} size="sm" />}
              onClick={() => setRevealKeyInSnippet((v) => !v)}
            />
            {/* isWrapped : le <pre> est en overflow-x hidden, donc sans retour à la
                ligne les lignes longues (URL + clé) sont coupées SANS moyen de
                les lire ni de les sélectionner. */}
            <CodeBlock
              isWrapped
              code={
                selectedKey && selectedModel
                  ? buildSnippet(tool, data.public_api_url, selectedKey, selectedModel, data.model_limits, revealKeyInSnippet)
                  : ""
              }
              language={snippetLanguage(tool)}
              width="100%"
            />
          </VStack>
        </Card>
      )}
    </VStack>
  );
}
