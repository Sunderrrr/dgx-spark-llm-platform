"use client";

import { useState } from "react";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { TextInput } from "@astryxdesign/core/TextInput";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import {
  QuestionMarkCircleIcon,
  PencilIcon,
  PaperAirplaneIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CheckCircleIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";

type AskQ = { question: string; options: string[] };

// The model's clarifying questions, shown one at a time (stepper).
// PLUSIEURS réponses par question : chaque option se coche et se décoche, et la
// réponse libre peut s'ajouter aux options choisies — une contrainte à un seul
// choix obligeait à trancher là où la vraie réponse est « les deux ».
// « Précédent » revient corriger ; sur la dernière question, « Envoyer les
// réponses » les soumet toutes d'un coup. Une fois envoyées, `answered` fige la
// carte en simple accusé de réception.
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
  const [step, setStep] = useState(0);
  const [chosen, setChosen] = useState<string[][]>(() => questions.map(() => []));
  const [otherOpen, setOtherOpen] = useState<boolean[]>(() => questions.map(() => false));
  const [otherText, setOtherText] = useState<string[]>(() => questions.map(() => ""));

  const set = <T,>(arr: T[], i: number, v: T) => arr.map((x, j) => (j === i ? v : x));
  // La réponse d'une question = les options cochées, plus le texte libre s'il y
  // en a un. Les deux peuvent coexister.
  const effective = (i: number) =>
    [...chosen[i], otherOpen[i] ? otherText[i].trim() : ""].filter(Boolean).join(" + ");
  const currentAnswered = effective(step) !== "";
  const allAnswered = questions.every((_, i) => effective(i) !== "");
  const isLast = step === questions.length - 1;

  // Bascule : recliquer une option la retire.
  const toggleOption = (opt: string) => {
    setChosen((a) =>
      set(a, step, a[step].includes(opt) ? a[step].filter((x) => x !== opt) : [...a[step], opt]));
  };
  const pickOther = () => setOtherOpen((o) => set(o, step, !o[step]));

  // Once submitted, just confirm — the answers are sent to the model but not
  // echoed in the chat (the user doesn't want to see them).
  if (answered) {
    return (
      <Card>
        <HStack gap={2} vAlign="center">
          <Icon icon={CheckCircleIcon} size="sm" color="accent" />
          <Text color="secondary">{t("Réponses envoyées")}</Text>
        </HStack>
      </Card>
    );
  }

  const q = questions[step];
  return (
    <Card>
      <VStack gap={4}>
        {questions.length > 1 ? (
          <VStack gap={1}>
            <Text type="supporting" color="secondary">
              {t("Question")} {step + 1} / {questions.length}
            </Text>
            <ProgressBar label={t("Progression")} isLabelHidden value={((step + 1) / questions.length) * 100} />
          </VStack>
        ) : null}

        <VStack gap={1}>
          <HStack gap={2} vAlign="start">
            <Icon icon={QuestionMarkCircleIcon} size="sm" color="accent" />
            <Text weight="semibold">{q.question}</Text>
          </HStack>
          <Text type="supporting" color="secondary">
            {t("Plusieurs réponses possibles.")}
          </Text>
        </VStack>

        <VStack gap={2}>
          {q.options.map((opt, oi) => (
            <SelectableCard
              key={oi}
              label={opt}
              padding={3}
              isSelected={chosen[step].includes(opt)}
              onChange={() => toggleOption(opt)}
            >
              <Text>{opt}</Text>
            </SelectableCard>
          ))}
          {otherOpen[step] ? (
            <TextInput
              label={t("Ta réponse")}
              isLabelHidden
              value={otherText[step]}
              onChange={(v) => setOtherText((a) => set(a, step, v))}
              placeholder={t("Ta réponse…")}
              size="sm"
            />
          ) : (
            <SelectableCard label={t("Autre…")} padding={3} isSelected={false} onChange={pickOther}>
              <HStack gap={2} vAlign="center">
                <Icon icon={PencilIcon} size="sm" color="secondary" />
                <Text color="secondary">{t("Autre…")}</Text>
              </HStack>
            </SelectableCard>
          )}
        </VStack>

        <HStack vAlign="center">
          {step > 0 ? (
            <Button
              label={t("Précédent")}
              variant="ghost"
              size="sm"
              icon={<Icon icon={ChevronLeftIcon} size="sm" />}
              onClick={() => setStep((s) => s - 1)}
            />
          ) : null}
          <StackItem size="fill" />
          {isLast ? (
            <Button
              label={t("Envoyer les réponses")}
              variant="primary"
              size="sm"
              isDisabled={!allAnswered}
              icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
              onClick={() => onSubmit(questions.map((_, i) => effective(i)))}
            />
          ) : (
            <Button
              label={t("Suivant")}
              variant="primary"
              size="sm"
              isDisabled={!currentAnswered}
              icon={<Icon icon={ChevronRightIcon} size="sm" />}
              onClick={() => setStep((s) => s + 1)}
            />
          )}
        </HStack>
      </VStack>
    </Card>
  );
}
