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

// Les neuf états du paquet `thinking-orbs`. L'orbite était figée sur
// « working » : à chaque nouvelle phase de réflexion on en tire une au hasard,
// pour que deux réponses ne s'animent pas de la même façon. Le tirage se fait
// dans l'initialiseur — l'indicateur n'apparaît qu'après l'envoi, donc jamais
// dans le HTML servi, et il n'y a aucun rendu serveur à faire diverger.
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

/** Le libellé, lettre par lettre, avec la MÊME vague que la grille de l'avatar
 *  généré : chaque lettre respire en décalé, la vague traverse le mot.
 *  Volontairement en opacité seule (pas de translation) : un mot dont les
 *  lettres montent et descendent devient pénible à lire. Les lettres doivent
 *  être des éléments distincts pour porter chacune son retard — d'où les
 *  `<span>`, qui restent en ligne (le mot n'est pas découpé pour l'écran). */
function LibelleAnime({ texte }: { texte: string }) {
  return (
    <Text type="supporting" color="secondary">
      {Array.from(texte).map((caractere, i) => (
        <span
          key={`${i}-${caractere}`}
          className="thinking-lettre"
          // Retard négatif : la vague est déjà répartie au premier rendu.
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
      {/* `thinking-orbs` (paquet npm, canvas) : orbe de particules à l'échelle
          « inline » de 20 px, état tiré au hasard ci-dessus. Le thème est passé
          EXPLICITEMENT depuis le mode de l'application : `auto` seul ne suffit
          pas, car Astryx ne pose `data-theme` que pour le mode sombre — un mode
          clair forcé sur un système sombre retomberait sur
          `prefers-color-scheme` et dessinerait une encre claire sur fond clair.
          « system » reste `auto`, là c'est exactement la bonne source. Le paquet
          gère lui-même `prefers-reduced-motion` (image fixe). Décoratif : le
          sens est porté par le role="status" ci-dessus, on le retire de l'arbre
          d'accessibilité pour ne pas l'annoncer deux fois. */}
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
