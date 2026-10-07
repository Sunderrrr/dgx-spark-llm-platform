"use client";

// The playground settings panel: fixed overlay anchored under the gear
// button. Extracted VERBATIM from app/(app)/playground/page.tsx (plain props,
// no behaviour change) — the page keeps the open/position wiring.

import { Icon } from "@astryxdesign/core/Icon";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { XMarkIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Settings } from "@/lib/types";
import { SettingsPanel } from "./SettingsPanel";

export type SettingsOverlayProps = {
  settingsPos: { top: number; right: number; maxH: number; largeur: number };
  setIsSettingsOpen: (v: boolean) => void;
  settings: Settings;
  setSettings: (s: Settings) => void;
  contexte: number | undefined;
  systemProvenance: "persona" | "skill" | "manual";
  setSystemProvenance: (p: "persona" | "skill" | "manual") => void;
};

export function SettingsOverlay({
  settingsPos,
  setIsSettingsOpen,
  settings,
  setSettings,
  contexte,
  systemProvenance,
  setSystemProvenance,
}: SettingsOverlayProps) {
  const t = useT();
  return (
            <HStack
              className="playground-settings-panel"
              style={{ position: "fixed", top: settingsPos.top, right: settingsPos.right,
                       width: settingsPos.largeur, zIndex: 30 }}
            >
              <Card style={{ width: "100%" }}>
                <VStack gap={2}>
                  <HStack vAlign="center">
                    <StackItem size="fill">
                      <Text weight="semibold">{t("Réglages du playground")}</Text>
                    </StackItem>
                    <Button
                      label={t("Fermer")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={XMarkIcon} size="sm" />}
                      onClick={() => setIsSettingsOpen(false)}
                    />
                  </HStack>
                  <VStack gap={3} style={{ overflowY: "auto", maxHeight: settingsPos.maxH }} isScrollable>
                    <SettingsPanel
                      settings={settings}
                      onChange={setSettings}
                      contexte={contexte}
                      provenance={systemProvenance}
                      onProvenance={setSystemProvenance}
                    />
                  </VStack>
                </VStack>
              </Card>
            </HStack>
  );
}
