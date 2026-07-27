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

type ApiKey = { key_alias: string; key: string; created_at: string; spend: number };
type Account = { spend: number; max_budget: number; budget_reset_at: string | null; unlimited: boolean; has_pending: boolean };
type KeysData = {
  user_keys: ApiKey[];
  budget_tokens: string;
  budget_duration: string;
  account: Account;
  model_limits: Record<string, ModelLimit>;
  running_models: string[];
  public_api_url: string;
};

export function KeysContent() {
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

  function refresh() {
    getJSON<KeysData>("/api/keys").then((d) => {
      setData(d);
      if (!selectedKey && d.user_keys.length) setSelectedKey(d.user_keys[0].key);
      if (!selectedModel && d.running_models.length) setSelectedModel(d.running_models[0]);
    });
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps -- ne doit tourner qu'au montage
  useEffect(refresh, []);

  async function createKey() {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "create", key_name: keyName });
    setKeyName("");
    showToast({ body: "Clé créée !", type: "info" });
    refresh();
  }

  async function revokeKey(key: string) {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "revoke", key });
    showToast({ body: "Clé révoquée.", type: "info" });
    refresh();
  }

  async function requestBudget() {
    if (!csrf) return;
    await postForm("/keys", csrf, { action: "request_budget", reason: budgetReason });
    setBudgetReason("");
    setShowBudgetForm(false);
    showToast({ body: "Demande de tokens envoyée !", type: "info" });
    refresh();
  }

  const columns: TableColumn<ApiKey & Record<string, unknown>>[] = [
    { key: "key_alias", header: "Alias" },
    {
      key: "key",
      header: "Clé",
      renderCell: (row) => (
        <HStack gap={2} vAlign="center">
          <Text type="code" hasTabularNumbers>
            {revealed.has(row.key) ? row.key : `${row.key.slice(0, 10)}…${row.key.slice(-4)}`}
          </Text>
          <Button
            label="Afficher"
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
    { key: "spend", header: "Dépensé", renderCell: (row) => `${Math.round(row.spend || 0).toLocaleString("fr-FR")} tokens` },
    {
      key: "actions" as keyof ApiKey,
      header: "",
      renderCell: (row) => (
        <Button label="Révoquer" variant="ghost" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => revokeKey(row.key)} />
      ),
    },
  ];

  const pct = data && !data.account.unlimited && data.account.max_budget ? (data.account.spend / data.account.max_budget) * 100 : 0;

  return (
    <VStack gap={5} maxWidth={980}>
      <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
        <VStack gap={1}>
          <Heading level={1}>Mes clés API</Heading>
          <Text type="supporting" color="secondary">
            Des clés personnelles pour appeler les modèles via l&apos;API compatible OpenAI.
          </Text>
        </VStack>
        <HStack gap={2} vAlign="end">
          <TextInput
            label="Nom"
            isLabelHidden
            value={keyName}
            onChange={setKeyName}
            placeholder="Nom (ex: mon-laptop)"
            size="sm"
          />
          <Button label="Nouvelle clé" variant="primary" size="sm" icon={<Icon icon={PlusIcon} size="sm" />} onClick={createKey} />
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
                <Badge label="Budget illimité (admin)" variant="warning" />
              </HStack>
            ) : (
              <VStack gap={1}>
                <HStack hAlign="between">
                  <Text type="supporting" color="secondary">
                    Budget du compte — partagé par toutes tes clés / {data.budget_duration}
                  </Text>
                  <Text type="supporting" color="secondary">
                    {Math.round(data.account.spend).toLocaleString("fr-FR")} / {Math.round(data.account.max_budget).toLocaleString("fr-FR")} tokens
                  </Text>
                </HStack>
                <ProgressBar
                  label="Budget"
                  isLabelHidden
                  value={Math.min(pct, 100)}
                  variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                />
                <HStack gap={3} vAlign="center">
                  <Text type="supporting" color="secondary">
                    {Math.max(Math.round(data.account.max_budget - data.account.spend), 0).toLocaleString("fr-FR")} tokens restants
                  </Text>
                  {data.account.has_pending ? (
                    <Badge label="Demande en attente" variant="neutral" />
                  ) : (
                    <Button
                      label="Demander plus de tokens"
                      variant="secondary"
                      size="sm"
                      onClick={() => setShowBudgetForm((v) => !v)}
                    />
                  )}
                </HStack>
                {showBudgetForm && (
                  <HStack gap={2}>
                    <TextInput label="Raison" isLabelHidden value={budgetReason} onChange={setBudgetReason} placeholder="Raison (optionnel)" size="sm" />
                    <Button label="Envoyer" variant="secondary" size="sm" onClick={requestBudget} />
                  </HStack>
                )}
              </VStack>
            )}
          </VStack>
        </Card>
      )}

      {data && data.user_keys.length === 0 && (
        <EmptyState
          icon={<Icon icon={KeyIcon} size="lg" />}
          title="Aucune clé pour l'instant."
          description="Utilise « Nouvelle clé » en haut à droite pour en générer une."
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
            <Text weight="semibold">Intégrations</Text>
            <HStack gap={3} wrap="wrap">
              <Selector label="Clé" value={selectedKey} onChange={(v) => setSelectedKey(v ?? "")} options={data.user_keys.map((k) => ({ value: k.key, label: k.key_alias }))} />
              <Selector label="Modèle" value={selectedModel} onChange={(v) => setSelectedModel(v ?? "")} options={data.running_models} />
            </HStack>
            <TabList value={tool} onChange={(v) => setTool(v as IntegrationTool)}>
              {INTEGRATION_TOOLS.map((t) => (
                <Tab key={t.value} value={t.value} label={t.label} />
              ))}
            </TabList>
            <Button
              label={revealKeyInSnippet ? "Masquer la clé" : "Révéler la clé"}
              variant="secondary"
              size="sm"
              icon={<Icon icon={revealKeyInSnippet ? EyeSlashIcon : EyeIcon} size="sm" />}
              onClick={() => setRevealKeyInSnippet((v) => !v)}
            />
            <CodeBlock
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
