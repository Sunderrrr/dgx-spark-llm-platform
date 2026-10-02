"use client";

import { useEffect, useState } from "react";
import { HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { ThinkingOrb, type OrbState } from "thinking-orbs";
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

// The nine states of the `thinking-orbs` package. The orbit was frozen on
// « working »: at each new thinking phase we draw one at random, so two
// answers do not animate the same way. The draw happens in the initializer —
// the indicator only appears after sending, so never in the served HTML, and
// there is no server render to diverge.
const ETATS: OrbState[] = [
  "working",
  "searching",
  "solving",
  "listening",
  "connecting",
  "weaving",
  "composing",
  "breathing",
  "shaping",
];

/** The label, letter by letter, with the SAME wave as the generated avatar
 *  grid: each letter breathes out of phase, the wave crosses the word.
 *  Deliberately opacity-only (no translation): a word whose letters go up
 *  and down becomes tedious to read. The letters must be distinct elements
 *  to each carry their own delay — hence the `<span>`s, which stay inline
 *  (the word is not split for the screen). */
function LibelleAnime({ texte }: { texte: string }) {
  return (
    <Text type="supporting" color="secondary">
      {Array.from(texte).map((caractere, i) => (
        <span
          key={`${i}-${caractere}`}
          className="thinking-lettre"
          // Negative delay: the wave is already spread out on first render.
          style={{ animationDelay: `-${((i % 12) * 0.11).toFixed(2)}s` }}
        >
          {caractere === " " ? "\u00a0" : caractere}
        </span>
      ))}
    </Text>
  );
}

export function ThinkingIndicator({ fixedLabel }: { fixedLabel?: string }) {
  const t = useT();
  const { mode } = useThemeMode();
  const [verb, setVerb] = useState(() => VERBS[Math.floor(Math.random() * VERBS.length)]);
  const [etat] = useState(() => ETATS[Math.floor(Math.random() * ETATS.length)]);

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
      {/* `thinking-orbs` (npm package, canvas): particle orb at the 20 px
          « inline » scale, state drawn at random above. The theme is passed
          EXPLICITLY from the application mode: `auto` alone is not enough,
          because Astryx only sets `data-theme` for dark mode — a forced
          light mode on a dark system would fall back to
          `prefers-color-scheme` and draw light ink on a light background.
          « system » stays `auto`, there it is exactly the right source. The
          package handles `prefers-reduced-motion` itself (still image).
          Decorative: the meaning is carried by the role="status" above, we
          remove it from the accessibility tree so it is not announced twice. */}
      <ThinkingOrb
        state={etat}
        size={20}
        theme={mode === "system" ? "auto" : mode}
        aria-hidden
      />
      <LibelleAnime texte={label} />
    </HStack>
  );
}
