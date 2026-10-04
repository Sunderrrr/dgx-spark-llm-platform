"use client";

/**
 * ReasoningBlock — le bloc de raisonnement, rendu façon LM Studio.
 *
 * Le rendu visé (captures de référence LM Studio) : la pensée vit dans un
 * PANNEAU dépliable (fond + liseré + coins arrondis) — en-tête « Réflexion »
 * avec la DURÉE réelle, corps atténué — et la réponse, elle, reste du texte
 * nu sous le panneau. Le panneau est OUVERT pendant la réflexion (on regarde
 * le modèle travailler) et se REPLIE de lui-même dès que l'écriture de la
 * réponse commence : c'est CE repli qui marque la fin de la réflexion.
 */
import { useEffect, useRef, useState } from "react";
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
  if (!reasoning) return null;
  const secondes = ms && ms >= 1000 ? Math.round(ms / 1000) : null;
  return (
    <VStack className="raison-panneau">
      <Collapsible
        isOpen={open}
        onOpenChange={setOpen}
        trigger={
          <HStack gap={2} vAlign="center" className="raison-entete">
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
    </VStack>
  );
}
