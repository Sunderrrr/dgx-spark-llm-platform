"use client";

/**
 * ReasoningBlock — le bloc de raisonnement, rendu façon LM Studio.
 *
 * Avant (2026-10-03) : un `Collapsible` nu avec un `Markdown` dedans — plat,
 * et illisible dès que la pensée est longue (ni durée, ni limite visuelle
 * entre la pensée et la réponse). Le rendu visé : un en-tête dépliable
 * (chevron + « Réflexion » + la DURÉE réelle mesurée sur le flux), et un
 * corps atténué, marqué par un liseré — la pensée accompagne la réponse,
 * elle ne la concurrence pas. Ouvert pendant la réflexion, replié après.
 */
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Text } from "@astryxdesign/core/Text";
import { HStack } from "@astryxdesign/core/HStack";
import { VStack } from "@astryxdesign/core/VStack";
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
  streaming?: boolean;
}) {
  const t = useT();
  if (!reasoning) return null;
  const secondes = ms && ms >= 1000 ? Math.round(ms / 1000) : null;
  return (
    <Collapsible
      // While thinking, the block is OPEN — the user watches it work; once
      // done it folds back behind the answer (LM Studio behaviour).
      defaultIsOpen={!!streaming}
      trigger={
        <HStack gap={2} vAlign="center">
          <Text type="supporting" color="secondary">
            {streaming && !secondes ? t("Réflexion en cours…") : t("Réflexion")}
          </Text>
          {secondes ? (
            <Text type="supporting" color="secondary" className="raison-duree">
              {t("pensé {n} s").replace("{n}", String(secondes))}
            </Text>
          ) : null}
        </HStack>
      }
    >
      <VStack className="raison-corps">
        <Markdown isStreaming={streaming}>{reasoning}</Markdown>
      </VStack>
    </Collapsible>
  );
}
