"use client";

import { useEffect, useState } from "react";
import { HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { useT } from "@/lib/i18n";

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
      <Text type="supporting" color="secondary">
        {label}
      </Text>
      <span style={{ display: "inline-flex", gap: 2, alignItems: "flex-end" }}>
        <span className="thinking-dot" style={{ animationDelay: "0s" }} />
        <span className="thinking-dot" style={{ animationDelay: "0.25s" }} />
        <span className="thinking-dot" style={{ animationDelay: "0.5s" }} />
      </span>
    </HStack>
  );
}
