"use client";

import { useEffect, useState } from "react";
import { HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";

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

export function ThinkingIndicator() {
  const [verb, setVerb] = useState(() => VERBS[Math.floor(Math.random() * VERBS.length)]);

  useEffect(() => {
    const id = setInterval(() => {
      setVerb((prev) => {
        const options = VERBS.filter((v) => v !== prev);
        return options[Math.floor(Math.random() * options.length)];
      });
    }, 2200);
    return () => clearInterval(id);
  }, []);

  return (
    <HStack gap={1} vAlign="center" role="status" aria-label={`${verb}…`}>
      <Text type="supporting" color="secondary">
        {verb}
      </Text>
      <span style={{ display: "inline-flex", gap: 2, alignItems: "flex-end" }}>
        <span className="thinking-dot" style={{ animationDelay: "0s" }} />
        <span className="thinking-dot" style={{ animationDelay: "0.15s" }} />
        <span className="thinking-dot" style={{ animationDelay: "0.3s" }} />
      </span>
    </HStack>
  );
}
