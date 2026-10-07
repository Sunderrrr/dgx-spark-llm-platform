"use client";

// « Utilisation » section of the settings dialog. Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx (plain props, no behaviour change).

import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Divider } from "@astryxdesign/core/Divider";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { ArrowPathIcon } from "@heroicons/react/24/outline";
import { useT, useLocale } from "@/lib/i18n";
import type { Limit } from "./types";
import { fmt } from "./format";

export type UsageSectionProps = {
  limits: Limit[];
  refresh: () => void;
};

export function UsageSection({ limits, refresh }: UsageSectionProps) {
  const t = useT();
  const numLocale = useLocale();
  return (
                <VStack gap={4}>
                  <HStack hAlign="between" vAlign="start" gap={3}>
                    <VStack gap={0}>
                      <Text weight="semibold">{t("Vos limites d'utilisation")}</Text>
                      <Text type="supporting" color="secondary">{t("Suivez la consommation de votre compte sur chaque quota disponible.")}</Text>
                    </VStack>
                    <Button
                      label={t("Rafraîchir")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                      onClick={refresh}
                    />
                  </HStack>
                  <VStack gap={4}>
                    {limits.map((l) => {
                      const pourcent =
                        l.unlimited || !l.max || l.used === null
                          ? null
                          : Math.min(100, Math.round((l.used / l.max) * 100));
                      return (
                        <HStack key={l.key} gap={4} vAlign="center" hAlign="between">
                          <VStack gap={0} width="45%">
                            <Text weight="semibold">{t(l.label)}</Text>
                            <Text type="supporting" color="secondary">
                              {t(l.desc)}
                            </Text>
                          </VStack>
                          <HStack gap={3} vAlign="center" width="50%">
                            {pourcent === null ? (
                              <Badge
                                label={l.unlimited ? t("Illimité") : `${fmt(l.max ?? 0, numLocale)} ${l.unit}`}
                                variant={l.unlimited ? "warning" : "neutral"}
                              />
                            ) : (
                              <>
                                <ProgressBar
                                  label={t(l.label)}
                                  isLabelHidden
                                  value={pourcent}
                                  variant={pourcent >= 90 ? "error" : pourcent >= 70 ? "warning" : "success"}
                                />
                                <Text type="supporting" color="secondary" hasTabularNumbers>
                                  {pourcent} {t("% utilisé")}
                                </Text>
                              </>
                            )}
                          </HStack>
                        </HStack>
                      );
                    })}
                  </VStack>
                  <Divider />
                  <Text type="supporting" color="secondary">
                    {t("Besoin d'augmenter tes limites ? Demande plus de tokens depuis l'onglet « Clés API », ou passe par l'assistant Support.")}
                  </Text>
                </VStack>
  );
}
