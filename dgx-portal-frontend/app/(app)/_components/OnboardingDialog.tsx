"use client";

import { useState } from "react";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Card } from "@astryxdesign/core/Card";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import {
  KeyIcon,
  ChatBubbleLeftRightIcon,
  CommandLineIcon,
  SparklesIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { useSettingsDialog } from "@/lib/settings-dialog";

/** One step of the onboarding. `action` is optional: it takes the
 *  user where they can do what was just explained. */
type Step = {
  icon: typeof KeyIcon;
  title: string;
  body: string;
  action?: { label: string; kind: "keys" | "playground" | "request" };
};

const STEPS: Step[] = [
  {
    icon: KeyIcon,
    title: "1. Crée ta clé API",
    body:
      "Tout passe par elle : le Playground comme tes outils externes. Elle porte ton budget en tokens, elle est personnelle, et tu peux en créer plusieurs (une par machine, par exemple) pour en révoquer une sans casser les autres.",
    action: { label: "Ouvrir mes clés API", kind: "keys" },
  },
  {
    icon: ChatBubbleLeftRightIcon,
    title: "2. Discute dans le Playground",
    body:
      "Le moyen le plus direct d'utiliser un modèle : rien à installer. Tu peux joindre des fichiers texte, suivre le débit en tokens/seconde, et le modèle te posera des questions s'il a besoin de précisions. Les fichiers qu'il écrit s'ouvrent dans un volet à côté.",
    action: { label: "Aller au Playground", kind: "playground" },
  },
  {
    icon: CommandLineIcon,
    title: "3. Branche tes propres outils",
    body:
      "L'API est compatible OpenAI et Anthropic. Dans Réglages → Clés API, choisis ton outil (Claude Code, OpenCode, Cursor, Aider, cURL…) : la configuration est générée avec ta clé, prête à copier. Vise le modèle « auto-model » et tu n'auras rien à changer quand l'admin changera de modèle.",
    action: { label: "Voir les intégrations", kind: "keys" },
  },
  {
    icon: SparklesIcon,
    title: "4. Besoin d'autre chose ?",
    body:
      "Image, voix, musique, OCR et vidéo ont chacun leur page. Si le modèle qu'il te faut n'est pas là, demande-le : un administrateur reçoit la demande et peut l'ajouter. Et le Support répond à tes questions sur la plateforme.",
    action: { label: "Demander un modèle", kind: "request" },
  },
];

export function OnboardingDialog({
  isOpen,
  onClose,
  firstName,
}: {
  isOpen: boolean;
  onClose: () => void;
  firstName?: string;
}) {
  const t = useT();
  const { open: openSettings } = useSettingsDialog();
  const [i, setI] = useState(0);
  const step = STEPS[i];
  const last = i === STEPS.length - 1;

  function act(kind: NonNullable<Step["action"]>["kind"]) {
    onClose();
    if (kind === "keys") openSettings("keys");
    else window.location.href = kind === "playground" ? "/playground" : "/request";
  }

  return (
    <Dialog isOpen={isOpen} onOpenChange={(o) => { if (!o) onClose(); }} width={620}>
      <DialogHeader
        title={firstName ? `${t("Bienvenue")}, ${firstName}` : t("Bienvenue")}
        subtitle={t("Trois minutes pour savoir quoi faire de cette plateforme.")}
        hasDivider
        onOpenChange={(o) => { if (!o) onClose(); }}
      />
      <VStack gap={4} padding={4}>
        <VStack gap={1}>
          <ProgressBar
            label={t("Progression")}
            isLabelHidden
            value={i + 1}
            max={STEPS.length}
          />
          <Text type="supporting" color="secondary">
            {t("Étape")} {i + 1} / {STEPS.length}
          </Text>
        </VStack>

        <Card variant="muted" padding={4}>
          <VStack gap={3}>
            <HStack gap={2} vAlign="center">
              <Icon icon={step.icon} size="md" color="accent" />
              <Text type="large" weight="semibold">{t(step.title)}</Text>
            </HStack>
            <Text color="secondary">{t(step.body)}</Text>
            {step.action && (
              <HStack>
                <Button
                  label={t(step.action.label)}
                  variant="secondary"
                  size="sm"
                  onClick={() => act(step.action!.kind)}
                />
              </HStack>
            )}
          </VStack>
        </Card>

        <HStack hAlign="between" vAlign="center" gap={2}>
          <Button label={t("Passer")} variant="ghost" size="sm" onClick={onClose} />
          <HStack gap={2}>
            {i > 0 && (
              <Button label={t("Précédent")} variant="secondary" size="sm" onClick={() => setI(i - 1)} />
            )}
            <Button
              label={last ? t("C'est parti") : t("Suivant")}
              variant="primary"
              size="sm"
              onClick={() => (last ? onClose() : setI(i + 1))}
            />
          </HStack>
        </HStack>
        <StackItem />
      </VStack>
    </Dialog>
  );
}
