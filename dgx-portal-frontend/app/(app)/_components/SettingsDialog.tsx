"use client";

import { useCallback, useEffect, useState } from "react";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { TabList, Tab } from "@astryxdesign/core/TabList";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { Grid } from "@astryxdesign/core/Grid";
import { Avatar } from "@astryxdesign/core/Avatar";
import { useToast } from "@astryxdesign/core/Toast";
import {
  KeyIcon,
  PlusIcon,
  TrashIcon,
  ServerStackIcon,
  SparklesIcon,
  UserCircleIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, postForm, postFormJSON } from "@/lib/api";
import { KeysContent } from "../keys/_components/KeysContent";

type McpServer = { id: number; name: string; url: string; has_auth: number; created_at: string };
type Skill = { id: number; name: string; description: string; created_at: string };
type AvatarChoice = { id: string; label: string };
type SettingsData = {
  mcp_servers: McpServer[];
  skills: Skill[];
  avatar_id: string | null;
  avatars: AvatarChoice[];
};

type SettingsTab = "keys" | "mcp" | "skills" | "avatar";

export function SettingsDialog({
  isOpen,
  onOpenChange,
  onAvatarChange,
}: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onAvatarChange?: (avatarId: string) => void;
}) {
  const csrf = useCsrf();
  const showToast = useToast();
  const [tab, setTab] = useState<SettingsTab>("keys");
  const [data, setData] = useState<SettingsData | null>(null);
  const [mcpName, setMcpName] = useState("");
  const [mcpUrl, setMcpUrl] = useState("");
  const [mcpAuth, setMcpAuth] = useState("");
  const [skillName, setSkillName] = useState("");
  const [skillDescription, setSkillDescription] = useState("");
  const [skillInstructions, setSkillInstructions] = useState("");

  const refresh = useCallback(() => {
    getJSON<SettingsData>("/api/settings").then(setData).catch(() => {});
  }, []);

  // Chargé à l'ouverture (et pas au montage) : le dialogue vit dans le layout,
  // donc monté en permanence — sans ça on ferait un appel API sur chaque page.
  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

  async function addMcpServer() {
    if (!csrf || !mcpName.trim() || !mcpUrl.trim()) return;
    const result = await postFormJSON<{ ok: boolean; error?: string; tool_count?: number }>(
      "/mcp",
      csrf,
      { action: "create", name: mcpName, url: mcpUrl, auth_header: mcpAuth },
    );
    if (result.ok) {
      setMcpName("");
      setMcpUrl("");
      setMcpAuth("");
      showToast({ body: `Serveur MCP connecté (${result.tool_count ?? 0} outil(s) trouvé(s)).`, type: "info" });
      refresh();
    } else {
      showToast({ body: result.error || "Échec de la connexion au serveur MCP.", type: "error" });
    }
  }

  async function deleteMcpServer(id: number) {
    if (!csrf) return;
    await postForm("/mcp", csrf, { action: "delete", id: String(id) });
    showToast({ body: "Serveur MCP supprimé.", type: "info" });
    refresh();
  }

  async function addSkill() {
    if (!csrf || !skillName.trim() || !skillDescription.trim() || !skillInstructions.trim()) return;
    const result = await postFormJSON("/skills", csrf, {
      action: "create",
      name: skillName,
      description: skillDescription,
      instructions: skillInstructions,
    });
    if (!result.ok) {
      showToast({ body: result.error || "Échec de l'enregistrement.", type: "error" });
      return;
    }
    setSkillName("");
    setSkillDescription("");
    setSkillInstructions("");
    showToast({ body: "Compétence enregistrée.", type: "info" });
    refresh();
  }

  async function deleteSkill(id: number) {
    if (!csrf) return;
    await postForm("/skills", csrf, { action: "delete", id: String(id) });
    showToast({ body: "Compétence supprimée.", type: "info" });
    refresh();
  }

  async function selectAvatar(avatarId: string) {
    if (!csrf) return;
    await postForm("/settings/avatar", csrf, { avatar_id: avatarId });
    setData((prev) => (prev ? { ...prev, avatar_id: avatarId } : prev));
    onAvatarChange?.(avatarId);
  }

  const mcpColumns: TableColumn<McpServer & Record<string, unknown>>[] = [
    { key: "name", header: "Nom" },
    { key: "url", header: "URL", renderCell: (r) => <Text type="code">{r.url}</Text> },
    {
      key: "has_auth",
      header: "Auth",
      renderCell: (r) => (r.has_auth ? <Badge label="Configurée" variant="success" /> : <Badge label="Aucune" variant="neutral" />),
    },
    {
      key: "id" as keyof McpServer,
      header: "",
      renderCell: (r) => (
        <Button label="Supprimer" variant="ghost" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => deleteMcpServer(r.id)} />
      ),
    },
  ];

  const skillColumns: TableColumn<Skill & Record<string, unknown>>[] = [
    { key: "name", header: "Nom" },
    { key: "description", header: "Description" },
    {
      key: "id" as keyof Skill,
      header: "",
      renderCell: (r) => (
        <Button label="Supprimer" variant="ghost" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => deleteSkill(r.id)} />
      ),
    },
  ];

  return (
    <Dialog isOpen={isOpen} onOpenChange={onOpenChange} purpose="form" width={820} maxHeight="82vh">
      <Layout
        header={
          <DialogHeader
            title="Réglages"
            subtitle="Clés API, serveurs MCP, compétences et personnalisation."
            onOpenChange={() => onOpenChange(false)}
          />
        }
        content={
          <LayoutContent>
        <VStack gap={4}>
          <TabList value={tab} onChange={(v) => setTab((v as SettingsTab) ?? "keys")}>
            <Tab value="keys" label="Clés API" icon={<Icon icon={KeyIcon} size="sm" />} />
            <Tab value="mcp" label="MCP" icon={<Icon icon={ServerStackIcon} size="sm" />} />
            <Tab value="skills" label="Compétences" icon={<Icon icon={SparklesIcon} size="sm" />} />
            <Tab value="avatar" label="Personnalisation" icon={<Icon icon={UserCircleIcon} size="sm" />} />
          </TabList>

          {tab === "keys" && <KeysContent />}

          {tab === "mcp" && (
            <VStack gap={4}>
              <Text type="supporting" color="secondary">
                Connecte un serveur MCP (Model Context Protocol) distant en HTTPS : ses outils deviennent
                utilisables par l&apos;assistant Support, visibles pendant la conversation.
              </Text>
              <Card>
                <VStack gap={3}>
                  <Text weight="semibold">Ajouter un serveur</Text>
                  <HStack gap={2} wrap="wrap">
                    <TextInput label="Nom" isLabelHidden value={mcpName} onChange={setMcpName} placeholder="Nom (ex: mon-serveur)" size="sm" />
                    <TextInput label="URL" isLabelHidden value={mcpUrl} onChange={setMcpUrl} placeholder="https://exemple.com/mcp" size="sm" />
                    <TextInput label="Authorization (optionnel)" isLabelHidden value={mcpAuth} onChange={setMcpAuth} placeholder="Bearer sk-..." size="sm" />
                    <Button label="Ajouter" variant="primary" size="sm" icon={<Icon icon={PlusIcon} size="sm" />} onClick={addMcpServer} />
                  </HStack>
                </VStack>
              </Card>
              {data && data.mcp_servers.length === 0 ? (
                <EmptyState
                  icon={<Icon icon={ServerStackIcon} size="lg" />}
                  title="Aucun serveur MCP connecté."
                  description="Ajoute une URL de serveur MCP distant ci-dessus."
                />
              ) : (
                data && (
                  <Card padding={0}>
                    <Table<McpServer & Record<string, unknown>> data={data.mcp_servers} columns={mcpColumns} idKey="id" density="balanced" dividers="rows" />
                  </Card>
                )
              )}
            </VStack>
          )}

          {tab === "skills" && (
            <VStack gap={4}>
              <Text type="supporting" color="secondary">
                Une compétence est un ensemble d&apos;instructions réutilisables que tu écris toi-même ;
                l&apos;assistant peut la charger en cours de conversation quand elle est utile à ta demande.
              </Text>
              <Card>
                <VStack gap={3}>
                  <Text weight="semibold">Ajouter une compétence</Text>
                  <HStack gap={2} wrap="wrap">
                    <TextInput label="Nom" isLabelHidden value={skillName} onChange={setSkillName} placeholder="Nom" size="sm" />
                    <TextInput label="Description" isLabelHidden value={skillDescription} onChange={setSkillDescription} placeholder="Description courte" size="sm" />
                  </HStack>
                  <TextArea
                    label="Instructions"
                    isLabelHidden
                    value={skillInstructions}
                    onChange={setSkillInstructions}
                    placeholder="Instructions détaillées que l'assistant chargera en contexte..."
                    rows={6}
                  />
                  <Button label="Enregistrer" variant="primary" size="sm" icon={<Icon icon={PlusIcon} size="sm" />} onClick={addSkill} />
                </VStack>
              </Card>
              {data && data.skills.length === 0 ? (
                <EmptyState
                  icon={<Icon icon={SparklesIcon} size="lg" />}
                  title="Aucune compétence pour l'instant."
                  description="Ajoute-en une ci-dessus."
                />
              ) : (
                data && (
                  <Card padding={0}>
                    <Table<Skill & Record<string, unknown>> data={data.skills} columns={skillColumns} idKey="id" density="balanced" dividers="rows" />
                  </Card>
                )
              )}
            </VStack>
          )}

          {tab === "avatar" && (
            <VStack gap={3}>
              <Text type="supporting" color="secondary">
                Choisis un avatar parmi les logos proposés — pas d&apos;import d&apos;image personnelle.
              </Text>
              <Grid columns={{ minWidth: 110, max: 5 }} gap={3}>
                {data?.avatars.map((a) => (
                  <SelectableCard
                    key={a.id}
                    label={a.label}
                    isSelected={data.avatar_id === a.id}
                    onChange={() => selectAvatar(a.id)}
                    padding={3}>
                    <VStack gap={2} hAlign="center">
                      <Avatar src={`/avatars/${a.id}.svg`} name={a.label} size="lg" />
                      <Text type="supporting" color="secondary">
                        {a.label}
                      </Text>
                    </VStack>
                  </SelectableCard>
                ))}
              </Grid>
            </VStack>
          )}
        </VStack>
          </LayoutContent>
        }
      />
    </Dialog>
  );
}
