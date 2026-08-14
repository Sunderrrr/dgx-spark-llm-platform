"use client";

import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { TextInput } from "@astryxdesign/core/TextInput";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { QuestionMarkCircleIcon, PencilIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";

// Renders a model's clarifying question with clickable answers (single choice
// + a free-text "Other"). Clicking an answer calls onAnswer immediately.
// `answered` disables the whole block once the user has replied.
export function AskQuestion({
  question,
  options,
  answered,
  onAnswer,
}: {
  question: string;
  options: string[];
  answered: boolean;
  onAnswer: (text: string) => void;
}) {
  const t = useT();
  const [otherOpen, setOtherOpen] = useState(false);
  const [other, setOther] = useState("");

  return (
    <Card>
      <VStack gap={3}>
        <HStack gap={2} vAlign="start">
          <Icon icon={QuestionMarkCircleIcon} size="sm" color="accent" />
          <Text weight="semibold">{question}</Text>
        </HStack>
        <VStack gap={2}>
          {options.map((opt, i) => (
            <ClickableCard
              key={i}
              label={opt}
              variant="muted"
              isDisabled={answered}
              onClick={() => onAnswer(opt)}
            >
              <Text>{opt}</Text>
            </ClickableCard>
          ))}
          {!answered && !otherOpen && (
            <Button
              label={t("Autre…")}
              variant="ghost"
              size="sm"
              icon={<Icon icon={PencilIcon} size="sm" />}
              onClick={() => setOtherOpen(true)}
            />
          )}
          {!answered && otherOpen && (
            <HStack gap={2}>
              <TextInput
                label={t("Ta réponse")}
                isLabelHidden
                value={other}
                onChange={setOther}
                placeholder={t("Ta réponse…")}
                size="sm"
              />
              <Button
                label={t("Envoyer")}
                variant="primary"
                size="sm"
                isDisabled={!other.trim()}
                onClick={() => onAnswer(other)}
              />
            </HStack>
          )}
        </VStack>
      </VStack>
    </Card>
  );
}
