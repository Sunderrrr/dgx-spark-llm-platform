"use client";

// « Apparence » section of the settings dialog (theme, accent colour,
// language). Extracted VERBATIM from app/(app)/_components/SettingsDialog.tsx
// (plain props, no behaviour change).

import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Icon } from "@astryxdesign/core/Icon";
import { Grid } from "@astryxdesign/core/Grid";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { SunIcon, MoonIcon, ComputerDesktopIcon } from "@heroicons/react/24/outline";
import { useT, type Lang } from "@/lib/i18n";
import { THEMES, type ThemeId } from "@/lib/themes";

export type AppearanceSectionProps = {
  mode: "light" | "dark" | "system";
  setMode: (m: "light" | "dark" | "system") => void;
  themeId: ThemeId;
  selectTheme: (id: ThemeId) => void;
  lang: Lang;
  selectLang: (l: Lang) => void;
};

export function AppearanceSection({
  mode,
  setMode,
  themeId,
  selectTheme,
  lang,
  selectLang,
}: AppearanceSectionProps) {
  const t = useT();
  return (
                <VStack gap={5}>
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">{t("THÈME")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("Ajuste l'apparence de l'interface.")}
                    </Text>
                    <Grid columns={3} gap={3}>
                      {[
                        { id: "light", label: "Clair", icon: SunIcon },
                        { id: "dark", label: "Sombre", icon: MoonIcon },
                        { id: "system", label: "Système", icon: ComputerDesktopIcon },
                      ].map((opt) => (
                        <SelectableCard
                          key={opt.id}
                          label={t(opt.label)}
                          isSelected={mode === opt.id}
                          onChange={() => setMode(opt.id as "light" | "dark" | "system")}
                          padding={3}>
                          <VStack gap={2} hAlign="center">
                            <Icon icon={opt.icon} size="md" color="secondary" />
                            <Text weight="semibold">{t(opt.label)}</Text>
                          </VStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">
                      {t("COULEUR D'ACCENT")}
                    </Text>
                    <Text type="supporting" color="secondary">
                      {t("Change la couleur principale de l'interface.")}
                    </Text>
                    <Grid columns={{ minWidth: 92, max: 5 }} gap={3}>
                      {THEMES.map((th) => (
                        <SelectableCard
                          key={th.id}
                          label={t(th.label)}
                          isSelected={themeId === th.id}
                          onChange={() => selectTheme(th.id)}
                          padding={3}>
                          <VStack gap={2} hAlign="center">
                            {/* Preview swatch: the only place where a raw
                                color is legitimate — it's the sample
                                itself, not a themed interface element. */}
                            <span
                              aria-hidden="true"
                              style={{
                                width: 28,
                                height: 28,
                                borderRadius: "50%",
                                background: th.swatch,
                                border: "1px solid var(--color-border)",
                              }}
                            />
                            <Text type="supporting" color="secondary">
                              {t(th.label)}
                            </Text>
                          </VStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>

                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">
                      {t("LANGUE")}
                    </Text>
                    <Text type="supporting" color="secondary">
                      {t("Choisis la langue de l'interface.")}
                    </Text>
                    <Grid columns={2} gap={3}>
                      {([
                        { id: "fr", label: t("Français"), drapeau: "🇫🇷" },
                        { id: "en", label: t("Anglais"), drapeau: "🇬🇧" },
                      ] as const).map((l) => (
                        <SelectableCard
                          key={l.id}
                          label={l.label}
                          isSelected={lang === l.id}
                          onChange={() => selectLang(l.id)}
                          padding={3}>
                          <HStack gap={2} vAlign="center">
                            <Text>{l.drapeau}</Text>
                            <Text weight="semibold">{l.label}</Text>
                          </HStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>
                </VStack>
  );
}
