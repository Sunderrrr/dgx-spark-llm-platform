"use client";

import { useState } from "react";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { TrashIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Skill } from "@/lib/skills";

/** Créateur de compétence : nom + commande /alias + prompt à envoyer +
 *  prompt système optionnel. Liste et supprime les compétences créées. */
export function SkillCreator({
  open,
  onOpenChange,
  customSkills,
  onSave,
  onDelete,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  customSkills: Skill[];
  onSave: (s: Skill) => void;
  onDelete: (id: string) => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [alias, setAlias] = useState("");
  const [description, setDescription] = useState("");
  const [prompt, setPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const canSave = Boolean(name.trim()) && Boolean(prompt.trim());

  function save() {
    if (!name.trim() || !prompt.trim()) return;
    onSave({
      id: "s" + Date.now(),
      name: name.trim(),
      alias: (alias.trim() || name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "comp"),
      description: description.trim(),
      prompt: prompt.trim(),
      systemPrompt: systemPrompt.trim() || undefined,
    });
    setName("");
    setAlias("");
    setDescription("");
    setPrompt("");
    setSystemPrompt("");
  }

  return (
    <Dialog isOpen={open} onOpenChange={onOpenChange} width={560}>
      <DialogHeader
        title={t("Créer un skill")}
        subtitle={t("Une compétence = un prompt à envoyer + une commande / pour l'appeler")}
        hasDivider
        onOpenChange={onOpenChange}
      />
      <VStack padding={3} gap={3}>
        <TextInput label={t("Nom")} value={name} onChange={setName} placeholder={t("Ex : Résumer des PDF")} />
        <TextInput label={t("Commande (optionnel)")} value={alias} onChange={setAlias} placeholder={t("Ex : resumer")} />
        <TextInput
          label={t("Description (optionnel)")}
          value={description}
          onChange={setDescription}
          placeholder={t("Ce que fait cette compétence")}
        />
        <TextArea
          label={t("Prompt à envoyer")}
          value={prompt}
          onChange={setPrompt}
          rows={2}
          placeholder={t("Ex : Résume ce document…")}
        />
        <TextArea
          label={t("System prompt (optionnel)")}
          value={systemPrompt}
          onChange={setSystemPrompt}
          rows={2}
          placeholder={t("Ex : Tu es un assistant spécialisé…")}
        />
        <HStack hAlign="end" gap={2}>
          <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => onOpenChange(false)} />
          <Button label={t("Créer le skill")} variant="primary" size="sm" isDisabled={!canSave} onClick={save} />
        </HStack>
        {customSkills.length > 0 && (
          <VStack gap={1} height={240} isScrollable>
            <Text type="supporting" color="secondary">{t("Mes compétences")}</Text>
            {customSkills.map((s) => (
              <HStack key={s.id} gap={2} vAlign="center">
                <StackItem size="fill">
                  <Card variant="muted" padding={2}>
                    <VStack gap={1}>
                      <HStack gap={2} vAlign="center" wrap="wrap">
                        <Text weight="semibold">{s.name}</Text>
                        <Badge label={`/${s.alias}`} variant="info" />
                      </HStack>
                      <Text maxLines={1} type="supporting" color="secondary">
                        {s.description || s.prompt}
                      </Text>
                    </VStack>
                  </Card>
                </StackItem>
                <Button
                  label={t("Supprimer")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={TrashIcon} size="sm" />}
                  onClick={() => onDelete(s.id)}
                />
              </HStack>
            ))}
          </VStack>
        )}
      </VStack>
    </Dialog>
  );
}
