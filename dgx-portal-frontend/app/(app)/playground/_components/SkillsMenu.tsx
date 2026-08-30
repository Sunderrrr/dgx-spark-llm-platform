"use client";

import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { PlusIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { type Skill, skillMatches } from "@/lib/skills";

/** Menu des compétences affiché sous le champ quand l'utilisateur tape « / ». */
export function SkillsMenu({
  skills,
  query,
  onSelect,
  onCreate,
}: {
  skills: Skill[];
  query: string;
  onSelect: (s: Skill) => void;
  onCreate: () => void;
}) {
  const t = useT();
  const hits = skills.filter((s) => skillMatches(s, query));
  return (
    <Card
      variant="muted"
      padding={3}
      style={{ border: "var(--border-width) solid var(--color-border-emphasized)" }}>
      <VStack gap={2}>
        <HStack hAlign="between" vAlign="center" gap={2}>
          <Text weight="semibold">{t("Compétences")}</Text>
          <Text type="supporting" color="secondary">
            {query ? `/${query}` : t("Tapez / pour appeler une compétence")}
          </Text>
        </HStack>
        <VStack gap={1} height={240} isScrollable>
          {hits.length === 0 ? (
            <Text color="secondary">{t("Aucune compétence ne correspond")}</Text>
          ) : (
            hits.map((s) => (
              <HStack key={s.id} gap={2} vAlign="center">
                <StackItem size="fill">
                  <ClickableCard label={s.name} variant="muted" onClick={() => onSelect(s)}>
                    <HStack gap={2} vAlign="center" wrap="wrap">
                      <Badge label={`/${s.alias}`} variant="info" />
                      <Text maxLines={1} type="supporting" color="secondary">
                        {s.description}
                      </Text>
                    </HStack>
                  </ClickableCard>
                </StackItem>
              </HStack>
            ))
          )}
        </VStack>
        <HStack>
          <Button
            label={t("Créer un skill")}
            variant="ghost"
            size="sm"
            icon={<Icon icon={PlusIcon} size="sm" />}
            onClick={onCreate}
          />
        </HStack>
      </VStack>
    </Card>
  );
}
