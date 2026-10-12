"use client";

// « Compétences » section of the settings dialog (playground skills listing +
// assistant skills list and create/edit form). Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx (plain props, no behaviour change).

import type { Dispatch, SetStateAction } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { Card } from "@astryxdesign/core/Card";
import { Grid } from "@astryxdesign/core/Grid";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { useT } from "@/lib/i18n";
import type { Skill as SkillPlayground } from "@/lib/skills";
import type { SettingsData, SkillForm } from "./types";
import { SkillPackages } from "./SkillPackages";

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
  deleteSkill: (id: number, name: string) => void;
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
                  <Text weight="semibold">{t("Mes compétences")}</Text>
                  <SkillPackages
                    refreshKey={data}
                    onCreate={() => {
                      setEditingSkillId(null);
                      setSkillForm({ name: "", description: "", instructions: "" });
                      setIsAddingSkill(true);
                    }}
                    onEdit={(s) => {
                      setEditingSkillId(s.id);
                      setSkillForm({ name: s.name, description: s.description, instructions: s.instructions || "" });
                      setIsAddingSkill(true);
                    }}
                    onDelete={deleteSkill}
                  />
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
