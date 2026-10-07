"use client";

// « MCP » section of the settings dialog (server list + create/edit form).
// Extracted VERBATIM from app/(app)/_components/SettingsDialog.tsx (plain
// props, no behaviour change).

import type { Dispatch, SetStateAction } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Switch } from "@astryxdesign/core/Switch";
import { Grid } from "@astryxdesign/core/Grid";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import {
  PlusIcon,
  ServerStackIcon,
  PencilSquareIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { McpForm, SettingsData } from "./types";

export type McpSectionProps = {
  active: boolean;
  data: SettingsData | null;
  isAddingMcp: boolean;
  setIsAddingMcp: (v: boolean) => void;
  editingMcpId: number | null;
  setEditingMcpId: (v: number | null) => void;
  mcpForm: McpForm;
  setMcpForm: Dispatch<SetStateAction<McpForm>>;
  toggleMcp: (id: number, enabled: boolean) => void;
  deleteMcp: (id: number, nom: string) => void;
};

export function McpSection({
  active,
  data,
  isAddingMcp,
  setIsAddingMcp,
  editingMcpId,
  setEditingMcpId,
  mcpForm,
  setMcpForm,
  toggleMcp,
  deleteMcp,
}: McpSectionProps) {
  const t = useT();
  return (
    <>

              {active && !isAddingMcp && (
                <VStack gap={4}>
                  <HStack hAlign="between" vAlign="center" gap={3}>
                    <Text type="supporting" color="secondary">
                      {t("Connecte un serveur MCP distant en HTTPS : ses outils deviennent utilisables par l'assistant Support.")}
                    </Text>
                    <Button
                      label={t("Connecter un MCP")}
                      variant="primary"
                      size="sm"
                      icon={<Icon icon={PlusIcon} size="sm" />}
                      onClick={() => {
                        setEditingMcpId(null);
                        setMcpForm({ name: "", url: "", description: "", allowedTools: "", auth: "" });
                        setIsAddingMcp(true);
                      }}
                    />
                  </HStack>
                  {data && data.mcp_servers.length === 0 ? (
                    <EmptyState
                      icon={<Icon icon={ServerStackIcon} size="lg" />}
                      title={t("Aucun serveur MCP connecté.")}
                      description={t("Connecte un serveur pour étendre les capacités de l'assistant.")}
                    />
                  ) : (
                    <VStack gap={3}>
                      {data?.mcp_servers.map((s) => (
                        <Card key={s.id}>
                          <VStack gap={2}>
                            <HStack hAlign="between" vAlign="start" gap={3}>
                              <VStack gap={0}>
                                <HStack gap={2} vAlign="center">
                                  <Text weight="semibold">{s.name}</Text>
                                  {s.has_auth ? <Badge label={t("Auth")} variant="success" /> : null}
                                </HStack>
                                <Text type="supporting" color="secondary" wordBreak="break-all">
                                  {s.url}
                                </Text>
                              </VStack>
                              <HStack gap={2} vAlign="center">
                                <Switch
                                  label={t("Serveur activé")}
                                  isLabelHidden
                                  value={!!s.enabled}
                                  onChange={(v) => toggleMcp(s.id, v)}
                                />
                                <Button
                                  label={t("Modifier")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={PencilSquareIcon} size="sm" />}
                                  onClick={() => {
                                    setEditingMcpId(s.id);
                                    setMcpForm({
                                      name: s.name,
                                      url: s.url,
                                      description: s.description || "",
                                      allowedTools: s.allowed_tools || "",
                                      auth: "",
                                    });
                                    setIsAddingMcp(true);
                                  }}
                                />
                                <Button
                                  label={t("Supprimer")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={TrashIcon} size="sm" />}
                                  onClick={() => deleteMcp(s.id, s.name)}
                                />
                              </HStack>
                            </HStack>
                            {s.description && (
                              <Text type="supporting" color="secondary">
                                {s.description}
                              </Text>
                            )}
                            {s.allowed_tools && (
                              <Text type="supporting" color="secondary">
                                {t("Outils autorisés :")} {s.allowed_tools}
                              </Text>
                            )}
                          </VStack>
                        </Card>
                      ))}
                    </VStack>
                  )}
                </VStack>
              )}

              {active && isAddingMcp && (
                <VStack gap={4}>
                  <VStack gap={0}>
                    <Text weight="semibold">{editingMcpId ? t("Modifier le serveur MCP") : t("Connecter un MCP personnalisé")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("Configurez la connexion et la façon dont ses outils peuvent être utilisés.")}
                    </Text>
                  </VStack>
                  <Card>
                    <VStack gap={4}>
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Nom")}
                          value={mcpForm.name}
                          onChange={(v) => setMcpForm((f) => ({ ...f, name: v }))}
                          placeholder={t("Exemple : notion_workspace")}
                          description={t("Lettres, chiffres, underscores et tirets uniquement.")}
                        />
                        <TextInput
                          label={t("URL du serveur")}
                          value={mcpForm.url}
                          onChange={(v) => setMcpForm((f) => ({ ...f, url: v }))}
                          placeholder="https://mcp.example.com/sse"
                        />
                      </Grid>
                      <TextArea
                        label={t("Description (optionnel)")}
                        value={mcpForm.description}
                        onChange={(v) => setMcpForm((f) => ({ ...f, description: v }))}
                        placeholder={t("Ce que fournit ce serveur")}
                        rows={2}
                      />
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Outils autorisés (optionnel)")}
                          value={mcpForm.allowedTools}
                          onChange={(v) => setMcpForm((f) => ({ ...f, allowedTools: v }))}
                          placeholder="search, create_page, …"
                          description={t("Séparez par des virgules. Vide = tous les outils.")}
                        />
                        <TextInput
                          label={t("Autorisation (optionnel)")}
                          // A Bearer token is masked like a password: it
                          // stayed readable in clear text while typing (screen
                          // sharing, screenshot). It is never re-served by the
                          // server (`/api/settings` only exposes `has_auth`).
                          type="password"
                          value={mcpForm.auth}
                          onChange={(v) => setMcpForm((f) => ({ ...f, auth: v }))}
                          placeholder={t("Bearer token ou secret")}
                          description={
                            editingMcpId
                              ? t("Laisser vide pour conserver le secret actuel ; « - » pour le retirer.")
                              : t("Envoyé en en-tête Authorization.")
                          }
                        />
                      </Grid>
                    </VStack>
                  </Card>
                </VStack>
              )}
    </>
  );
}
