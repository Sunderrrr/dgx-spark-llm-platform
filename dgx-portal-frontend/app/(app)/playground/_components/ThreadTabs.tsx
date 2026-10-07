"use client";

// The playground tab bar (several conversations open). Extracted VERBATIM
// from app/(app)/playground/page.tsx (plain props, no behaviour change).

import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { PlusIcon, XMarkIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Tab } from "@/lib/playground-thread";

export type ThreadTabsProps = {
  tabs: Tab[];
  activeTabId: string;
  streaming: boolean;
  switchTab: (id: string) => void;
  closeTab: (id: string) => void;
  newTab: () => void;
};

export function ThreadTabs({ tabs, activeTabId, streaming, switchTab, closeTab, newTab }: ThreadTabsProps) {
  const t = useT();
  return (
    <>
          {/* Onglets : plusieurs conversations ouvertes. Basculement désactivé
              pendant un flux (l'état live est celui de la génération en cours). */}
          {tabs.length > 0 && (
            <VStack gap={1} padding={2}>
              <HStack gap={2} vAlign="center" wrap="wrap">
                {tabs.map((tb) => (
                  <HStack key={tb.id} gap={1} vAlign="center">
                    <Button
                      label={tb.title || t("Nouvelle conversation")}
                      variant={tb.id === activeTabId ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() => switchTab(tb.id)}
                    />
                    <Button
                      label={t("Fermer")}
                      variant="ghost"
                      size="sm"
                      // Closing a tab mid-generation was refused
                      // silently by `closeTab`: we show it.
                      isDisabled={streaming}
                      isIconOnly
                      icon={<Icon icon={XMarkIcon} size="sm" />}
                      onClick={() => closeTab(tb.id)}
                    />
                  </HStack>
                ))}
                <Button
                  label={t("Nouvel onglet")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  isDisabled={streaming}
                  onClick={newTab}
                />
              </HStack>
            </VStack>
          )}
    </>
  );
}
