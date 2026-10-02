"use client";

import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { BoltIcon, PencilIcon, TrashIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { type Skill } from "@/lib/skills";

/** Returns the card tint: blue when the row is selected at the keyboard. */
function cardVariant(isSelected: boolean): "muted" | "blue" {
  return isSelected ? "blue" : "muted";
}

/** A skill row: click to select, and for user-created skills, « Modifier » /
 *  « Supprimer » buttons (keeps the interactions independent). */
function SkillRow({
  skill,
  index,
  selectedIndex,
  onSelect,
  onEdit,
  onDelete,
}: {
  skill: Skill;
  index: number;
  selectedIndex: number;
  onSelect: (s: Skill) => void;
  onEdit: (s: Skill) => void;
  onDelete: (id: string) => void;
}) {
  const t = useT();
  return (
    <HStack gap={2} vAlign="center">
      <StackItem size="fill">
        <ClickableCard
          label={t(skill.name)}
          variant={cardVariant(index === selectedIndex)}
          onClick={() => onSelect(skill)}>
          <HStack gap={2} vAlign="center" wrap="wrap">
            <Badge label={`/${skill.alias}`} variant="info" />
            {skill.systemPrompt ? (
              <Badge label={t("Sys")} variant="warning" />
            ) : null}
            <Text maxLines={1} type="supporting" color="secondary">
              {t(skill.description)}
            </Text>
          </HStack>
        </ClickableCard>
      </StackItem>
      {!skill.builtin ? (
        <HStack gap={1} vAlign="center">
          <Button
            label={t("Modifier")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={PencilIcon} size="sm" />}
            onClick={() => onEdit(skill)}
          />
          <Button
            label={t("Supprimer")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={TrashIcon} size="sm" />}
            onClick={() => onDelete(skill.id)}
          />
        </HStack>
      ) : null}
    </HStack>
  );
}

/** Skills menu shown below the input when the user types " / ".
 *  Offers the base skills (the « Compétences » group) and the user-created ones
 *  (the « Mes compétences » group), plus the « Créer un skill » entry.
 *  The highlighted row matches keyboard navigation (arrow keys + Enter). */
export function SkillsMenu({
  baseSkills,
  customSkills,
  query,
  selectedIndex,
  onSelect,
  onCreate,
  onEdit,
  onDelete,
}: {
  baseSkills: Skill[];
  customSkills: Skill[];
  query: string;
  selectedIndex: number;
  onSelect: (s: Skill) => void;
  onCreate: () => void;
  onEdit: (s: Skill) => void;
  onDelete: (id: string) => void;
}) {
  const t = useT();
  const total = baseSkills.length + customSkills.length;
  const baseOffset = 1; // row 0 is the « Créer un skill » card
  const customOffset = baseOffset + baseSkills.length;
  return (
    <Card
      variant="muted"
      padding={3}
      style={{ border: "var(--border-width) solid var(--color-border-emphasized)" }}>
      <VStack gap={2}>
        <HStack hAlign="between" vAlign="center" gap={2}>
          <HStack gap={2} vAlign="center">
            <Icon icon={BoltIcon} size="sm" color="accent" />
            <Text weight="semibold">{t("Compétences")}</Text>
            <Badge label={String(total)} variant="info" />
          </HStack>
          <Text type="supporting" color="secondary">
            {query ? `/${query}` : t("Tapez / pour appeler une compétence")}
          </Text>
        </HStack>
        <VStack gap={1} height={260} isScrollable>
          <ClickableCard
            label={t("Créer un skill")}
            variant={cardVariant(selectedIndex === 0)}
            onClick={onCreate}>
            <HStack gap={2} vAlign="center" wrap="wrap">
              <Badge label="/skill-creator" variant="warning" />
              <Text maxLines={1} type="supporting" color="secondary">
                {t("Ajoute tes propres compétences")}
              </Text>
            </HStack>
          </ClickableCard>
          {total === 0 ? (
            <Text color="secondary">{t("Aucune compétence ne correspond")}</Text>
          ) : (
            <>
              <Text type="label" color="secondary">
                {t("Compétences")}
              </Text>
              {baseSkills.map((s, i) => (
                <SkillRow
                  key={s.id}
                  skill={s}
                  index={baseOffset + i}
                  selectedIndex={selectedIndex}
                  onSelect={onSelect}
                  onEdit={onEdit}
                  onDelete={onDelete}
                />
              ))}
              {customSkills.length > 0 ? (
                <>
                  <Text type="label" color="secondary">
                    {t("Mes compétences")}
                  </Text>
                  {customSkills.map((s, i) => (
                    <SkillRow
                      key={s.id}
                      skill={s}
                      index={customOffset + i}
                      selectedIndex={selectedIndex}
                      onSelect={onSelect}
                      onEdit={onEdit}
                      onDelete={onDelete}
                    />
                  ))}
                </>
              ) : null}
            </>
          )}
        </VStack>
        {customSkills.length > 0 ? (
          <Text type="supporting" color="secondary" maxLines={1}>
            {t("Certaines compétences remplacent le prompt système")}
          </Text>
        ) : null}
      </VStack>
    </Card>
  );
}
