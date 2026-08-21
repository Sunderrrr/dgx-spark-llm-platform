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

/** Une étape de la prise en main. `action` est facultative : elle emmène
 *  l'utilisateur là où il pourra faire ce qui vient d'être expliqué. */
type Etape = {
  icon: typeof KeyIcon;
  titre: string;
  corps: string;
  action?: { label: string; kind: "keys" | "playground" | "request" };
};

const ETAPES: Etape[] = [
  {
    icon: KeyIcon,
    titre: "1. Crée ta clé API",
    corps:
      "Tout passe par elle : le Playground comme tes outils externes. Elle porte ton budget en tokens, elle est personnelle, et tu peux en créer plusieurs (une par machine, par exemple) pour en révoquer une sans casser les autres.",
    action: { label: "Ouvrir mes clés API", kind: "keys" },
  },
  {
    icon: ChatBubbleLeftRightIcon,
    titre: "2. Discute dans le Playground",
    corps:
      "Le moyen le plus direct d'utiliser un modèle : rien à installer. Tu peux joindre des fichiers texte, suivre le débit en tokens/seconde, et le modèle te posera des questions s'il a besoin de précisions. Les fichiers qu'il écrit s'ouvrent dans un volet à côté.",
    action: { label: "Aller au Playground", kind: "playground" },
  },
  {
    icon: CommandLineIcon,
    titre: "3. Branche tes propres outils",
    corps:
      "L'API est compatible OpenAI et Anthropic. Dans Réglages → Clés API, choisis ton outil (Claude Code, OpenCode, Cursor, Aider, cURL…) : la configuration est générée avec ta clé, prête à copier. Vise le modèle « auto-model » et tu n'auras rien à changer quand l'admin changera de modèle.",
    action: { label: "Voir les intégrations", kind: "keys" },
  },
  {
    icon: SparklesIcon,
    titre: "4. Besoin d'autre chose ?",
    corps:
      "Image, voix, musique, OCR et vidéo ont chacun leur page. Si le modèle qu'il te faut n'est pas là, demande-le : un administrateur reçoit la demande et peut l'ajouter. Et le Support répond à tes questions sur la plateforme.",
    action: { label: "Demander un modèle", kind: "request" },
  },
];

export function OnboardingDialog({
  isOpen,
  onClose,
  prenom,
}: {
  isOpen: boolean;
  onClose: () => void;
  prenom?: string;
}) {
  const t = useT();
  const { open: openSettings } = useSettingsDialog();
  const [i, setI] = useState(0);
  const etape = ETAPES[i];
  const dernier = i === ETAPES.length - 1;

  function agir(kind: NonNullable<Etape["action"]>["kind"]) {
    onClose();
    if (kind === "keys") openSettings("keys");
    else window.location.href = kind === "playground" ? "/playground" : "/request";
  }

  return (
    <Dialog isOpen={isOpen} onOpenChange={(o) => { if (!o) onClose(); }} width={620}>
      <DialogHeader
        title={prenom ? `${t("Bienvenue")}, ${prenom}` : t("Bienvenue")}
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
            max={ETAPES.length}
          />
          <Text type="supporting" color="secondary">
            {t("Étape")} {i + 1} / {ETAPES.length}
          </Text>
        </VStack>

        <Card variant="muted" padding={4}>
          <VStack gap={3}>
            <HStack gap={2} vAlign="center">
              <Icon icon={etape.icon} size="md" color="accent" />
              <Text type="large" weight="semibold">{t(etape.titre)}</Text>
            </HStack>
            <Text color="secondary">{t(etape.corps)}</Text>
            {etape.action && (
              <HStack>
                <Button
                  label={t(etape.action.label)}
                  variant="secondary"
                  size="sm"
                  onClick={() => agir(etape.action!.kind)}
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
              label={dernier ? t("C'est parti") : t("Suivant")}
              variant="primary"
              size="sm"
              onClick={() => (dernier ? onClose() : setI(i + 1))}
            />
          </HStack>
        </HStack>
        <StackItem />
      </VStack>
    </Dialog>
  );
}
