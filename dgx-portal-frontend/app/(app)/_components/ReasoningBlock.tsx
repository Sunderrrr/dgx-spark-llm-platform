"use client";

/**
 * ReasoningBlock — the reasoning block, rendered LM Studio-style.
 *
 * The target rendering (reference screenshots in /home/mael/Images) comes
 * in two states, and the DIFFERENCE between the two is what makes the
 * style:
 *
 *   - collapsed: a single line laid on the page background — chevron,
 *     dimmed label, duration — without the slightest box around it. Exactly
 *     the « › Thought for 7.04 seconds » line from LM Studio;
 *   - expanded: the same line, and UNDER it a bubble (subtle background,
 *     border, corners) carrying the thought text. The bubble only exists
 *     expanded: « la réflexion dans une bulle si tu développes ».
 *
 * The reasoning → writing boundary can be SEEN too: while thinking, the
 * line carries a pulsing dot and a running timer (« Réflexion en
 * cours… 12 s »); at the first word of the answer the timer stops on the
 * real duration and the block collapses on its own. It is that collapse,
 * timer frozen, that says « he is writing now ».
 */
import { useEffect, useRef, useState } from "react";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Text } from "@astryxdesign/core/Text";
import { HStack } from "@astryxdesign/core/HStack";
import { VStack } from "@astryxdesign/core/VStack";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { useT } from "@/lib/i18n";

export function ReasoningBlock({
  reasoning,
  ms,
  streaming,
}: {
  reasoning: string;
  /** Thinking duration in ms (first reasoning chunk → first content chunk). */
  ms?: number;
  /** The thinking phase is running: no answer text yet. */
  streaming?: boolean;
}) {
  const t = useT();
  // Controlled so the fold follows the phase change (thinking → writing),
  // while a manual toggle still wins until the next phase change.
  const [open, setOpen] = useState(!!streaming);
  const phaseRef = useRef(!!streaming);
  useEffect(() => {
    if (phaseRef.current === !!streaming) return;
    phaseRef.current = !!streaming;
    setOpen(!!streaming);
  }, [streaming]);
  // Live clock while the model thinks: the final duration is only known when
  // the first answer word arrives. Until then the counter keeps moving — and
  // its STOP is exactly the thinking → writing boundary. The start time is
  // taken in the effect, never during render (`Date.now()` is impure there).
  const debut = useRef<number>(0);
  const [ecoule, setEcoule] = useState(0);
  useEffect(() => {
    if (!streaming) return;
    debut.current = Date.now();
    const id = setInterval(() => setEcoule(Date.now() - debut.current), 500);
    return () => clearInterval(id);
  }, [streaming]);
  if (!reasoning) return null;
  const duree = streaming ? ecoule : (ms ?? 0);
  const secondes = duree >= 1000 ? Math.round(duree / 1000) : null;
  return (
    <Collapsible
      isOpen={open}
      onOpenChange={setOpen}
      trigger={
        <HStack gap={2} vAlign="center" className="raison-entete">
          {streaming ? (
            <StatusDot variant="accent" isPulsing label={t("Réflexion en cours…")} />
          ) : null}
          <Text type="supporting" color="secondary">
            {streaming ? t("Réflexion en cours…") : t("Réflexion")}
          </Text>
          {secondes ? (
            <Text type="supporting" color="secondary" className="raison-duree">
              {streaming
                ? t("depuis {n} s").replace("{n}", String(secondes))
                : t("pensé {n} s").replace("{n}", String(secondes))}
            </Text>
          ) : null}
        </HStack>
      }
    >
      {/* La bulle : elle entoure la PENSÉE, jamais l'en-tête. */}
      <VStack className="raison-panneau">
        <VStack className="raison-corps">
          <Markdown isStreaming={streaming}>{reasoning}</Markdown>
        </VStack>
      </VStack>
    </Collapsible>
  );
}
