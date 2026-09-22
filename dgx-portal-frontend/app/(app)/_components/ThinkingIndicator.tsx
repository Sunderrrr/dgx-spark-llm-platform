"use client";

import { useEffect, useState } from "react";
import { HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { ThinkingOrb } from "thinking-orbs";
import { useT } from "@/lib/i18n";
import { useThemeMode } from "../../theme-provider";

// Rotates through the same kind of whimsical status verbs Claude Code shows
// while it works, so an empty streaming bubble doesn't read as broken.
const VERBS = [
  "Réflexion",
  "Cogitation",
  "Rumination",
  "Gamberge",
  "Mijotage",
  "Élucubration",
  "Ébullition",
  "Méditation",
  "Tergiversation",
  "Concoction",
];

// Each word stays on screen 10-15s (randomized so a long chat with several
// concurrent thinking bubbles doesn't visibly sync up) before rotating.
const MIN_WORD_MS = 10000;
const MAX_WORD_MS = 15000;

export function ThinkingIndicator({ fixedLabel }: { fixedLabel?: string }) {
  const t = useT();
  const { mode } = useThemeMode();
  const [verb, setVerb] = useState(() => VERBS[Math.floor(Math.random() * VERBS.length)]);

  useEffect(() => {
    if (fixedLabel) return;
    let id: ReturnType<typeof setTimeout>;
    const scheduleNext = () => {
      id = setTimeout(() => {
        setVerb((prev) => {
          const options = VERBS.filter((v) => v !== prev);
          return options[Math.floor(Math.random() * options.length)];
        });
        scheduleNext();
      }, MIN_WORD_MS + Math.random() * (MAX_WORD_MS - MIN_WORD_MS));
    };
    scheduleNext();
    return () => clearTimeout(id);
  }, [fixedLabel]);

  const label = fixedLabel ?? t(verb);

  return (
    <HStack gap={1} vAlign="center" role="status" aria-label={`${label}…`}>
      {/* `thinking-orbs` (paquet npm, canvas) : orbite de particules à
          l'échelle « inline » de 20 px. Le thème est passé EXPLICITEMENT depuis
          le mode de l'application : `auto` seul ne suffit pas, car Astryx ne
          pose `data-theme` que pour le mode sombre — un mode clair forcé sur un
          système sombre retomberait sur `prefers-color-scheme` et dessinerait
          une encre claire sur fond clair. « system » reste `auto`, là c'est
          exactement la bonne source. Le paquet gère lui-même
          `prefers-reduced-motion` (image fixe). Décoratif : le sens est porté
          par le role="status" ci-dessus, on le retire de l'arbre
          d'accessibilité pour ne pas l'annoncer deux fois. */}
      <ThinkingOrb
        state="working"
        size={20}
        theme={mode === "system" ? "auto" : mode}
        aria-hidden
      />
      <Text type="supporting" color="secondary">
        {label}
      </Text>
    </HStack>
  );
}
