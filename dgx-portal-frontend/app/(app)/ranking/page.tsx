"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { List, ListItem } from "@astryxdesign/core/List";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Badge } from "@astryxdesign/core/Badge";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { ChartBarIcon } from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

type RankRow = {
  rank: number;
  username: string;
  is_me: boolean;
  tokens: number;
  bar_pct: number;
  delta: number | null;
  prompt: number;
  completion: number;
};

type RankingData = {
  rows: RankRow[];
  active_count: number;
  period: string;
  period_label: string;
  prev_label: string;
};

const PERIODS = [
  { value: "day", label: "Jour" },
  { value: "week", label: "Semaine" },
  { value: "month", label: "Mois" },
];

const MEDALS: Record<number, string> = { 1: "🥇", 2: "🥈", 3: "🥉" };

export default function RankingPage() {
  const t = useT();
  const [period, setPeriod] = useState("day");
  const [data, setData] = useState<RankingData | null>(null);

  useEffect(() => {
    getJSON<RankingData>(`/api/ranking?period=${period}`).then(setData);
  }, [period]);

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5} maxWidth={860}>
            <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
              <VStack gap={1}>
                <Heading level={1}>{t("Classement")}</Heading>
                <Text type="supporting" color="secondary">{t("Qui consomme le plus, en tokens réellement consommés (prompt + généré).")}</Text>
              </VStack>
              <SegmentedControl label={t("Période")} value={period} onChange={setPeriod}>
                {PERIODS.map((p) => (
                  <SegmentedControlItem key={p.value} value={p.value} label={t(p.label)} />
                ))}
              </SegmentedControl>
            </HStack>

            <Card>
              <VStack gap={3}>
                <HStack hAlign="between" vAlign="center">
                  <Text weight="semibold">{data ? t(data.period_label) : ""}</Text>
                  {data && data.rows.length > 0 && (
                    <Text type="supporting" color="secondary">
                      {data.active_count} {t(data.active_count > 1 ? "comptes actifs" : "compte actif")}
                    </Text>
                  )}
                </HStack>

                {data && data.rows.length === 0 && (
                  <EmptyState
                    icon={<Icon icon={ChartBarIcon} size="lg" />}
                    title={t("Aucune consommation sur cette période.")}
                  />
                )}

                {data && data.rows.length > 0 && (
                  <List>
                    {data.rows.map((r) => (
                      <ListItem
                        key={r.username}
                        label={`${MEDALS[r.rank] ?? r.rank}  ${r.username}`}
                        description={<ProgressBar label={t("Consommation")} isLabelHidden value={r.bar_pct} />}
                        endContent={
                          <VStack gap={0} align="end">
                            <Text weight="bold" hasTabularNumbers>
                              {Math.round(r.tokens).toLocaleString("fr-FR")}
                            </Text>
                            {r.delta == null ? (
                              <Text type="supporting" color="secondary">
                                {t("nouveau")}
                              </Text>
                            ) : (
                              <Text type="supporting" color={r.delta >= 0 ? "accent" : "secondary"}>
                                {r.delta >= 0 ? "▲" : "▼"} {Math.abs(Math.round(r.delta))}%
                              </Text>
                            )}
                          </VStack>
                        }
                        startContent={r.is_me ? <Badge label={t("toi")} variant="neutral" /> : undefined}
                      />
                    ))}
                  </List>
                )}

                {data && data.rows.length > 0 && (
                  <Text type="supporting" color="secondary">
                    {t("Delta vs")} {t(data.prev_label)}. {t("Total = tokens prompt + générés.")}
                  </Text>
                )}
              </VStack>
            </Card>
          </VStack>
        </LayoutContent>
      }
    />
  );
}
