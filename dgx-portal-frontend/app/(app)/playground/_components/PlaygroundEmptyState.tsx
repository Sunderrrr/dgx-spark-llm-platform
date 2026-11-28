"use client";

// The first screen of the playground: the greeting and the composer, centered,
// before the first message. Extracted VERBATIM from the `isFirstEmpty` branch
// of PlaygroundPage.tsx (plain props, no behaviour change).
//
// No wrapper is added around the composer: the two effects that paint around
// its card (voice halo, border beam) read the card as their first child and
// rely on the clipping set in globals.css.

import type { ReactNode } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { SparklesIcon } from "@heroicons/react/24/outline";

export type PlaygroundEmptyStateProps = {
  greeting: string;
  /** The composer node, built by the page. */
  composer: ReactNode;
};

export function PlaygroundEmptyState({ greeting, composer }: PlaygroundEmptyStateProps) {
  return (
    <StackItem size="fill">
      <VStack width="100%" height="100%" vAlign="center" hAlign="center" gap={4} padding={4}>
        <HStack gap={2} vAlign="center">
          <Icon icon={SparklesIcon} size="lg" color="accent" />
          <Text type="display-2" as="h1">{greeting}</Text>
        </HStack>
        <HStack gap={4} width="100%" hAlign="center">
          <VStack maxWidth={720} width="100%">{composer}</VStack>
        </HStack>
      </VStack>
    </StackItem>
  );
}
