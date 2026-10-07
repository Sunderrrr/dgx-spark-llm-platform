"use client";

// « Avatar » section of the settings dialog. Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx (plain props, no behaviour change).

import { VStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Grid } from "@astryxdesign/core/Grid";
import { Avatar } from "@astryxdesign/core/Avatar";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { UserAvatar } from "@/lib/user-avatar";
import { useT } from "@/lib/i18n";
import type { Account, SettingsData } from "./types";

export type AvatarSectionProps = {
  data: SettingsData | null;
  acct: Account | undefined;
  selectAvatar: (avatarId: string) => void;
};

export function AvatarSection({ data, acct, selectAvatar }: AvatarSectionProps) {
  const t = useT();
  return (
                <VStack gap={3}>
                  <Text type="supporting" color="secondary">
                    {t("Par défaut, ton avatar est créé à partir de ton pseudo. Tu peux aussi choisir un logo de marque d'IA — pas d'import d'image personnelle.")}
                  </Text>
                  <Grid columns={{ minWidth: 110, max: 5 }} gap={3}>
                    {/* Waits for the settings: before, the handle is unknown and the
                        monogram would show « ? » while the request runs. */}
                    {data && (
                      <SelectableCard
                        key="genere"
                        label={t("Généré depuis mon pseudo")}
                        isSelected={!data.avatar_id}
                        onChange={() => selectAvatar("")}
                        padding={3}>
                        <VStack gap={2} hAlign="center">
                          <UserAvatar
                            username={data.account?.username}
                            name={acct?.fullname}
                            size="lg"
                          />
                          <Text type="supporting" color="secondary">
                            {t("Généré depuis mon pseudo")}
                          </Text>
                        </VStack>
                      </SelectableCard>
                    )}
                    {data?.avatars.map((a) => (
                      <SelectableCard
                        key={a.id}
                        label={a.label}
                        isSelected={data.avatar_id === a.id}
                        onChange={() => selectAvatar(a.id)}
                        padding={3}>
                        <VStack gap={2} hAlign="center">
                          <Avatar src={`/avatars/${a.id}.svg`} name={a.label} size="lg" />
                          <Text type="supporting" color="secondary">
                            {a.label}
                          </Text>
                        </VStack>
                      </SelectableCard>
                    ))}
                  </Grid>
                </VStack>
  );
}
