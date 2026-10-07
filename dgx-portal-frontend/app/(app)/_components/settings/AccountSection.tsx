"use client";

// « Mon compte » section of the settings dialog. Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx (plain props, no behaviour change).

import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Badge } from "@astryxdesign/core/Badge";
import { Grid } from "@astryxdesign/core/Grid";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { UserAvatar } from "@/lib/user-avatar";
import { useT, useLocale } from "@/lib/i18n";
import { ActivityHeatmap } from "../ActivityHeatmap";
import { SecurityContent } from "../SecurityContent";
import type { Account, Activity } from "./types";
import { fmt, compact } from "./format";

export type AccountSectionProps = {
  acct: Account;
  act: Activity;
  avatarId: string | null | undefined;
  pct: number;
};

export function AccountSection({ acct, act, avatarId, pct }: AccountSectionProps) {
  const t = useT();
  const numLocale = useLocale();
  return (
                <VStack gap={5}>
                  <VStack gap={2} hAlign="center">
                    <UserAvatar avatarId={avatarId} username={acct.username}
                                name={acct.fullname} size="xl" />
                    <Heading level={2}>{acct.fullname}</Heading>
                    <HStack gap={2} vAlign="center">
                      <Text type="supporting" color="secondary">
                        {acct.username}
                      </Text>
                      {acct.is_admin && <Badge label="Admin" variant="warning" />}
                    </HStack>
                  </VStack>

                  <Card padding={0}>
                    <Grid columns={4}>
                      {[
                        { v: compact(act.total), l: "Tokens totaux" },
                        { v: compact(act.peak), l: "Pic journalier" },
                        { v: String(act.active_days), l: "Jours actifs" },
                        { v: compact(act.avg), l: "Moyenne / jour" },
                      ].map((s) => (
                        <VStack key={s.l} gap={0} hAlign="center" padding={4}>
                          <Text size="xl" weight="bold" hasTabularNumbers>
                            {s.v}
                          </Text>
                          <Text type="supporting" color="secondary">
                            {t(s.l)}
                          </Text>
                        </VStack>
                      ))}
                    </Grid>
                  </Card>

                  <VStack gap={2}>
                    <HStack hAlign="between" vAlign="center">
                      <Text type="supporting" color="secondary">{t("ACTIVITÉ TOKENS")}</Text>
                      <Text type="supporting" color="secondary">{t("6 derniers mois")}</Text>
                    </HStack>
                    <Card>
                      <ActivityHeatmap days={act.days} />
                    </Card>
                  </VStack>

                  <Grid columns={2} gap={4}>
                    <VStack gap={2}>
                      <Text weight="semibold">{t("Insights d'activité")}</Text>
                      <VStack gap={1}>
                        {[
                          ["Total période", fmt(act.total, numLocale)],
                          ["Pic journalier", act.peak_day ? `${new Date(act.peak_day + "T00:00:00").toLocaleDateString(numLocale)} — ${fmt(act.peak, numLocale)}` : "—"],
                          ["Jours actifs", String(act.active_days)],
                        ].map(([l, v]) => (
                          <HStack key={l} hAlign="between" gap={3}>
                            <Text type="supporting" color="secondary">{t(l)}</Text>
                            <Text type="supporting" hasTabularNumbers>{v}</Text>
                          </HStack>
                        ))}
                      </VStack>
                    </VStack>
                    <VStack gap={2}>
                      <Text weight="semibold">{t("Répartition tokens")}</Text>
                      <VStack gap={1}>
                        {[
                          ["Entrée (prompt)", fmt(act.prompt, numLocale)],
                          ["Sortie (généré)", fmt(act.completion, numLocale)],
                          ["Clés API actives", String(acct.key_count)],
                        ].map(([l, v]) => (
                          <HStack key={l} hAlign="between" gap={3}>
                            <Text type="supporting" color="secondary">{t(l)}</Text>
                            <Text type="supporting" hasTabularNumbers>{v}</Text>
                          </HStack>
                        ))}
                      </VStack>
                    </VStack>
                  </Grid>

                  <VStack gap={2}>
                    <Text weight="semibold">{t("Budget")}</Text>
                    <Card>
                      {acct.unlimited ? (
                        <HStack>
                          <Badge label={t("Budget illimité (admin)")} variant="warning" />
                        </HStack>
                      ) : (
                        <VStack gap={2}>
                          <HStack hAlign="between">
                            <Text type="supporting" color="secondary">
                              {t("Consommé sur la période")}
                            </Text>
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {fmt(acct.spend, numLocale)} / {fmt(acct.max_budget || 0, numLocale)} tokens
                            </Text>
                          </HStack>
                          <ProgressBar
                            label={t("Budget")}
                            isLabelHidden
                            value={Math.min(pct, 100)}
                            variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                          />
                          {/* The counter restarts from zero at the LiteLLM
                              envelope reset date (weekly by default).
                              Showing it avoids believing in a daily quota
                              that never climbs back. */}
                          {acct.budget_reset_at ? (
                            <Text type="supporting" color="secondary">
                              {t("Remis à zéro le {date}.").replace(
                                "{date}", new Date(acct.budget_reset_at).toLocaleDateString(numLocale),
                              )}
                            </Text>
                          ) : null}
                        </VStack>
                      )}
                    </Card>
                  </VStack>

                  <SecurityContent />
                </VStack>
  );
}
