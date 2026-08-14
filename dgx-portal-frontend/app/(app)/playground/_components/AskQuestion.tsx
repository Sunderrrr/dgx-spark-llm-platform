"use client";

import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { TextInput } from "@astryxdesign/core/TextInput";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { Divider } from "@astryxdesign/core/Divider";
import { QuestionMarkCircleIcon, PencilIcon, PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";

type AskQ = { question: string; options: string[] };

// Renders one or several clarifying questions the model asked. Each answer is
// selectable and changeable (no send on click); a single "Send answers" button
// submits them all together. Once submitted, `answered` locks the card while
// keeping the chosen answers visible.
export function AskQuestion({
  questions,
  answered,
  onSubmit,
}: {
  questions: AskQ[];
  answered: boolean;
  onSubmit: (answers: string[]) => void;
}) {
  const t = useT();
  const [chosen, setChosen] = useState<(string | null)[]>(() => questions.map(() => null));
  const [otherOpen, setOtherOpen] = useState<boolean[]>(() => questions.map(() => false));
  const [otherText, setOtherText] = useState<string[]>(() => questions.map(() => ""));

  const set = <T,>(arr: T[], i: number, v: T) => arr.map((x, j) => (j === i ? v : x));
  const effective = (i: number) => (otherOpen[i] ? otherText[i].trim() : (chosen[i] ?? ""));
  const allAnswered = questions.every((_, i) => effective(i) !== "");

  const pickOption = (qi: number, opt: string) => {
    setChosen((a) => set(a, qi, opt));
    setOtherOpen((o) => set(o, qi, false));
  };
  const pickOther = (qi: number) => {
    setOtherOpen((o) => set(o, qi, true));
    setChosen((a) => set(a, qi, null));
  };

  return (
    <Card>
      <VStack gap={4}>
        {questions.map((q, qi) => (
          <VStack key={qi} gap={2}>
            {qi > 0 ? <Divider /> : null}
            <HStack gap={2} vAlign="start">
              <Icon icon={QuestionMarkCircleIcon} size="sm" color="accent" />
              <Text weight="semibold">{q.question}</Text>
            </HStack>
            <VStack gap={2}>
              {q.options.map((opt, oi) => (
                <SelectableCard
                  key={oi}
                  label={opt}
                  padding={3}
                  isDisabled={answered}
                  isSelected={!otherOpen[qi] && chosen[qi] === opt}
                  onChange={() => pickOption(qi, opt)}
                >
                  <Text>{opt}</Text>
                </SelectableCard>
              ))}
              {otherOpen[qi] ? (
                <TextInput
                  label={t("Ta réponse")}
                  isLabelHidden
                  value={otherText[qi]}
                  onChange={(v) => setOtherText((a) => set(a, qi, v))}
                  placeholder={t("Ta réponse…")}
                  size="sm"
                  isDisabled={answered}
                />
              ) : (
                <SelectableCard
                  label={t("Autre…")}
                  padding={3}
                  isDisabled={answered}
                  isSelected={false}
                  onChange={() => pickOther(qi)}
                >
                  <HStack gap={2} vAlign="center">
                    <Icon icon={PencilIcon} size="sm" color="secondary" />
                    <Text color="secondary">{t("Autre…")}</Text>
                  </HStack>
                </SelectableCard>
              )}
            </VStack>
          </VStack>
        ))}
        {!answered ? (
          <HStack hAlign="end">
            <Button
              label={t("Envoyer les réponses")}
              variant="primary"
              size="sm"
              isDisabled={!allAnswered}
              icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
              onClick={() => onSubmit(questions.map((_, i) => effective(i)))}
            />
          </HStack>
        ) : null}
      </VStack>
    </Card>
  );
}
