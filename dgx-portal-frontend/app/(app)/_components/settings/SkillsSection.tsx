"use client";

// « Compétences » section of the settings dialog (playground skills listing +
// assistant skills list and create/edit form). Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx (plain props, no behaviour change).

import type { Dispatch, SetStateAction } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Grid } from "@astryxdesign/core/Grid";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import {
  PlusIcon,
  SparklesIcon,
  PencilSquareIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Skill as SkillPlayground } from "@/lib/skills";
import type { SettingsData, SkillForm } from "./types";

export type SkillsSectionProps = {
  active: boolean;
  data: SettingsData | null;
  skillsPlayground: SkillPlayground[];
  isAddingSkill: boolean;
  setIsAddingSkill: (v: boolean) => void;
  editingSkillId: number | null;
  setEditingSkillId: (v: number | null) => void;
  skillForm: SkillForm;
  setSkillForm: Dispatch<SetStateAction<SkillForm>>;
  deleteSkill: (id: number, nom: string) => void;
};

export function SkillsSection({
  active,
  data,
  skillsPlayground,
  isAddingSkill,
  setIsAddingSkill,
  editingSkillId,
  setEditingSkillId,
  skillForm,
  setSkillForm,
  deleteSkill,
}: SkillsSectionProps) {
  const t = useT();
  return (
    <>

              {active && !isAddingSkill && (
                <VStack gap={4}>
                  {/* What the « / » menu offers — the defaults included. Read-
                      only here: a built-in is edited in the source, a personal
                      one from the playground's creator. */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Compétences du playground (menu « / »)")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("Appelées depuis le composeur en tapant « / ». Les intégrées sont livrées avec la plateforme ; les tiennes sont modifiables depuis le playground.")}
                    </Text>
                    <VStack gap={2}>
                      {skillsPlayground.map((s) => (
                        <Card key={s.id}>
                          <HStack gap={2} vAlign="center">
                            <Badge label={`/${s.alias}`} variant={s.builtin ? "info" : "neutral"} />
                            <VStack gap={0}>
                              <Text weight="semibold">{t(s.name)}</Text>
                              <Text type="supporting" color="secondary">{t(s.description)}</Text>
                            </VStack>
                          </HStack>
                        </Card>
                      ))}
                    </VStack>
                  </VStack>
                  <Text weight="semibold">{t("Compétences de l'assistant")}</Text>
                  <HStack hAlign="between" vAlign="center" gap={3}>
                    <Text type="supporting" color="secondary">
                      {t("Des instructions réutilisables que tu écris toi-même ; l'assistant les charge quand elles sont utiles à ta demande.")}
                    </Text>
                    <Button
                      label={t("Nouvelle compétence")}
                      variant="primary"
                      size="sm"
                      icon={<Icon icon={PlusIcon} size="sm" />}
                      onClick={() => {
                        setEditingSkillId(null);
                        setSkillForm({ name: "", description: "", instructions: "" });
                        setIsAddingSkill(true);
                      }}
                    />
                  </HStack>
                  {data && data.skills.length === 0 ? (
                    <EmptyState
                      icon={<Icon icon={SparklesIcon} size="lg" />}
                      title={t("Aucune compétence pour l'instant.")}
                      description={t("Crée une compétence pour guider l'assistant sur une tâche récurrente.")}
                    />
                  ) : (
                    <VStack gap={3}>
                      {data?.skills.map((s) => (
                        <Card key={s.id}>
                          <HStack hAlign="between" vAlign="start" gap={3}>
                            <VStack gap={0}>
                              <Text weight="semibold">{s.name}</Text>
                              <Text type="supporting" color="secondary">
                                {s.description}
                              </Text>
                            </VStack>
                            <HStack gap={1}>
                              <Button
                                label={t("Modifier")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={PencilSquareIcon} size="sm" />}
                                onClick={() => {
                                  setEditingSkillId(s.id);
                                  setSkillForm({
                                    name: s.name,
                                    description: s.description,
                                    instructions: s.instructions || "",
                                  });
                                  setIsAddingSkill(true);
                                }}
                              />
                              <Button
                                label={t("Supprimer")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={TrashIcon} size="sm" />}
                                onClick={() => deleteSkill(s.id, s.name)}
                              />
                            </HStack>
                          </HStack>
                        </Card>
                      ))}
                    </VStack>
                  )}
                </VStack>
              )}

              {active && isAddingSkill && (
                <VStack gap={4}>
                  <VStack gap={0}>
                    <Text weight="semibold">{editingSkillId ? t("Modifier la compétence") : t("Créer une compétence")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("L'assistant chargera ces instructions en contexte quand la compétence s'applique.")}
                    </Text>
                  </VStack>
                  <Card>
                    <VStack gap={4}>
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Nom")}
                          value={skillForm.name}
                          onChange={(v) => setSkillForm((f) => ({ ...f, name: v }))}
                          placeholder={t("Exemple : analyse-de-logs")}
                        />
                        <TextInput
                          label={t("Description")}
                          value={skillForm.description}
                          onChange={(v) => setSkillForm((f) => ({ ...f, description: v }))}
                          placeholder={t("Quand l'utiliser, en une phrase")}
                        />
                      </Grid>
                      <TextArea
                        label={t("Instructions")}
                        value={skillForm.instructions}
                        onChange={(v) => setSkillForm((f) => ({ ...f, instructions: v }))}
                        placeholder={t("Instructions détaillées que l'assistant chargera en contexte…")}
                        rows={10}
                      />
                    </VStack>
                  </Card>
                </VStack>
              )}
    </>
  );
}
