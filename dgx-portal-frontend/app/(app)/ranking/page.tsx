"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Badge } from "@astryxdesign/core/Badge";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { Table, proportional, pixel } from "@astryxdesign/core/Table";
import { UserAvatar } from "@/lib/user-avatar";
import type { TableColumn } from "@astryxdesign/core/Table";
import { ChartBarIcon } from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";

/* The ranking is about ACTUALLY consumed tokens (input + generated), not
 * about weighted cost, which underestimates prompt-heavy loads by a factor
 * of ~10. The two readings do not crown the same winner — measurement of
 * 2026-09-14 over 30 days: #1 concentrated 30.9 % of the total but 0.46 % of
 * the generated — so « total » mostly ranks those who SEND context. Hence
 * the metric selector: it goes back to the server, which re-sorts, recomputes
 * the share and the delta. The screen never re-sorts a ranking itself. */

interface RankRow extends Record<string, unknown> {
  rank: number | null;
  username: string;
  /** The chosen brand logo, or null for the avatar generated from the username. */
  avatar_id?: string | null;
  is_me: boolean;
  is_unattributed: boolean;
  value: number;
  tokens: number;
  prompt: number;
  completion: number;
  share_pct: number;
  delta: number | null;
  trend: number[];
}

type RankingData = {
  rows: RankRow[];
  active_count: number;
  period: string;
  metric: string;
  total: number;
  avg: number;
  has_prev: boolean;
  period_label: string;
  prev_label: string;
};

const PERIODS = [
  { value: "day", label: "Jour" },
  { value: "week", label: "Semaine" },
  { value: "month", label: "Mois" },
  { value: "year", label: "Année" },
  { value: "all", label: "Tout" },
];

const METRICS = [
  { value: "total", label: "Total" },
  { value: "completion", label: "Généré" },
  { value: "prompt", label: "Entrée" },
];

const MEDALS: Record<number, string> = { 1: "🥇", 2: "🥈", 3: "🥉" };

/** Percentage in the displayed language: « 30,89 % » in French, « 30.89% » in English. */
function pct(v: number, numLocale: string, decimales = 2) {
  return (v / 100).toLocaleString(numLocale, {
    style: "percent",
    minimumFractionDigits: decimales,
    maximumFractionDigits: decimales,
  });
}

/* Trend: the series is already computed server-side, one value per bucket
 * of the period (24 h, 7 j, 30 j or 12 months). Under 5 points we display
 * nothing rather than a curve: « depuis le debut » has only 3, and three
 * points say no more than the total. */
function Tendance({ points, myself }: { points: number[]; myself: boolean }) {
  const t = useT();
  if (points.length < 5) {
    return <Text type="supporting" color="secondary">—</Text>;
  }
  const W = 100;
  const H = 24;
  const max = Math.max(...points, 1);
  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * W;
      const y = H - 1 - (v / max) * (H - 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: "100%", height: H, display: "block" }}
      preserveAspectRatio="none"
      role="img"
      aria-label={t("Activité sur la période")}
    >
      <path
        d={d}
        style={{
          fill: "none",
          stroke: myself ? "var(--color-accent)" : "var(--color-text-secondary)",
          strokeWidth: 1.5,
          strokeLinejoin: "round",
          strokeLinecap: "round",
        }}
      />
    </svg>
  );
}

export default function RankingPage() {
  const t = useT();
  const numLocale = useLocale();
  const [period, setPeriod] = useState("day");
  const [metric, setMetric] = useState("total");
  const [state, setState] = useState<{ data: RankingData | null; failed: boolean }>({
    data: null,
    failed: false,
  });

  useEffect(() => {
    // An outage must be distinguishable from an absence of consumption: without
    // this state, an API error displayed « aucune consommation sur cette periode »
    // and led to the conclusion that nobody had used anything. The `vivant`
    // flag discards the answer of a period already left — on a fast round-trip
    // between two periods, the slowest overwrote the most recent.
    let vivant = true;
    getJSON<RankingData>(`/api/ranking?period=${period}&metric=${metric}`)
      .then((d) => { if (vivant) setState({ data: d, failed: false }); })
      .catch(() => { if (vivant) setState({ data: null, failed: true }); });
    return () => { vivant = false; };
  }, [period, metric]);

  const { data, failed } = state;
  const rows = data?.rows ?? [];
  const ranked = rows.filter((r) => !r.is_unattributed);
  const unattributed = rows.filter((r) => r.is_unattributed);
  const me = rows.find((r) => r.is_me);
  const metricLabel = t(metric === "completion" ? "Généré" : metric === "prompt" ? "Entrée" : "Total");
  const hasRows = ranked.length > 0;

  const columns: TableColumn<RankRow>[] = [
    {
      key: "rank",
      header: t("Rang"),
      width: pixel(64),
      renderCell: (r) => (
        <Text weight="bold" hasTabularNumbers color={r.is_unattributed ? "secondary" : undefined}>
          {r.rank ? MEDALS[r.rank] ?? String(r.rank) : "—"}
        </Text>
      ),
    },
    {
      key: "username",
      header: t("Compte"),
      width: proportional(2),
      renderCell: (r) => (
        <HStack gap={2} vAlign="center">
          {/* No avatar for unassigned keys: they are not accounts,
              and an invented creature would lend them a face. */}
          {!r.is_unattributed && (
            <UserAvatar avatarId={r.avatar_id} username={r.username} size="sm" />
          )}
          <Text weight={r.is_me ? "bold" : undefined} color={r.is_unattributed ? "secondary" : undefined}>
            {r.username === "inconnu" ? t("Clés non attribuées") : r.username}
          </Text>
          {r.is_me && <Badge label={t("toi")} variant="neutral" />}
        </HStack>
      ),
    },
    {
      key: "share_pct",
      header: t("Part du total"),
      width: proportional(2),
      renderCell: (r) => (
        <VStack gap={1}>
          <Text hasTabularNumbers size="sm" color={r.is_unattributed ? "secondary" : undefined}>
            {pct(r.share_pct, numLocale)}
          </Text>
          <ProgressBar label={t("Part du total")} isLabelHidden value={Math.min(100, r.share_pct)} />
        </VStack>
      ),
    },
    {
      key: "value",
      header: metricLabel,
      width: proportional(2),
      align: "end",
      renderCell: (r) => (
        <VStack gap={0} align="end">
          <Text weight="bold" hasTabularNumbers>{Math.round(r.value).toLocaleString(numLocale)}</Text>
          {/* The input/generated split was computed but never shown:
              yet it is what explains the ranking. */}
          {metric === "total" && (
            <Text type="supporting" color="secondary">
              {t("dont {n} générés").replace("{n}", Math.round(r.completion).toLocaleString(numLocale))}
            </Text>
          )}
        </VStack>
      ),
    },
    {
      key: "trend",
      header: t("Tendance"),
      width: pixel(112),
      renderCell: (r) => <Tendance points={r.trend} myself={r.is_me} />,
    },
    {
      key: "delta",
      header: data?.has_prev
        ? t("Évolution vs {p}").replace("{p}", t(data.prev_label))
        : t("Évolution"),
      width: pixel(104),
      align: "end",
      renderCell: (r) =>
        r.delta == null ? (
          <Text type="supporting" color="secondary">—</Text>
        ) : (
          <Text type="supporting" color={r.delta >= 0 ? "accent" : "secondary"} hasTabularNumbers>
            {r.delta >= 0 ? "▲" : "▼"} {pct(Math.abs(r.delta), numLocale, 0)}
          </Text>
        ),
    },
  ];

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5} maxWidth={1000}>
            <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
              <VStack gap={1}>
                <Heading level={1}>{t("Classement")}</Heading>
                <Text type="supporting" color="secondary">
                  {t("Tokens réellement consommés sur la période, par compte.")}
                </Text>
              </VStack>
              <SegmentedControl label={t("Période")} value={period} onChange={setPeriod}>
                {PERIODS.map((p) => (
                  <SegmentedControlItem key={p.value} value={p.value} label={t(p.label)} />
                ))}
              </SegmentedControl>
            </HStack>

            {/* Personal position: the only piece of information one came
                for and that so far had to be read off the list, sometimes
                fifteenth. */}
            {data && (
              <Card>
                {me ? (
                  <HStack gap={5} vAlign="center" wrap="wrap">
                    <VStack gap={0}>
                      <Text type="supporting" color="secondary">{t("Votre position")}</Text>
                      <Text weight="bold" size="lg" hasTabularNumbers>
                        {me.rank ? `#${me.rank}` : "—"}
                      </Text>
                    </VStack>
                    <VStack gap={0}>
                      <Text type="supporting" color="secondary">{metricLabel}</Text>
                      <Text weight="bold" hasTabularNumbers>
                        {Math.round(me.value).toLocaleString(numLocale)}
                      </Text>
                    </VStack>
                    <VStack gap={0}>
                      <Text type="supporting" color="secondary">{t("Part du total")}</Text>
                      <Text weight="bold" hasTabularNumbers>{pct(me.share_pct, numLocale)}</Text>
                    </VStack>
                    {data.has_prev && (
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{t("Évolution")}</Text>
                        <Text weight="bold" hasTabularNumbers>
                          {me.delta == null ? "—" : `${me.delta >= 0 ? "▲" : "▼"} ${pct(Math.abs(me.delta), numLocale, 0)}`}
                        </Text>
                      </VStack>
                    )}
                    <Text type="supporting" color="secondary">
                      {t("sur {n} comptes actifs").replace("{n}", String(data.active_count))}
                    </Text>
                  </HStack>
                ) : (
                  <Text type="supporting" color="secondary">
                    {t("Vous n'avez rien consommé sur cette période.")}
                  </Text>
                )}
              </Card>
            )}

            <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
              <SegmentedControl label={t("Métrique")} value={metric} onChange={setMetric}>
                {METRICS.map((m) => (
                  <SegmentedControlItem key={m.value} value={m.value} label={t(m.label)} />
                ))}
              </SegmentedControl>
              {data && hasRows && (
                <Text type="supporting" color="secondary">
                  {t(data.period_label)} · {data.active_count} {t(data.active_count > 1 ? "comptes actifs" : "compte actif")}
                  {" · "}{Math.round(data.total).toLocaleString(numLocale)} {t("tokens au total")}
                  {" · "}{Math.round(data.avg).toLocaleString(numLocale)} {t("en moyenne par compte")}
                </Text>
              )}
            </HStack>

            {failed && (
              <EmptyState
                icon={<Icon icon={ChartBarIcon} size="lg" />}
                title={t("Classement indisponible.")}
                description={t("La consommation n'a pas pu être lue. Réessaie dans un instant.")}
              />
            )}

            {!failed && data && !hasRows && (
              <EmptyState
                icon={<Icon icon={ChartBarIcon} size="lg" />}
                title={t("Aucune consommation sur cette période.")}
              />
            )}

            {/* Edge-to-edge table, no Card: dense data reads in rows,
                and a box around it only adds a frame. */}
            {!failed && hasRows && (
              <Table
                data={[...ranked, ...unattributed]}
                columns={columns}
                idKey="username"
                density="compact"
                hasHover
                verticalAlign="middle"
              />
            )}

            {!failed && data && hasRows && (
              <VStack gap={1}>
                <Text type="supporting" color="secondary">
                  {t("Total = entrée + généré.")}
                  {!data.has_prev && ` ${t("« Depuis le début » n'a pas de période précédente : aucune évolution à afficher.")}`}
                </Text>
                {unattributed.length > 0 && (
                  <Text type="supporting" color="secondary">
                    {t("Les lignes non attribuées ne correspondent à aucun compte (clés supprimées ou de test) : elles consomment sans être classées.")}
                  </Text>
                )}
              </VStack>
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
