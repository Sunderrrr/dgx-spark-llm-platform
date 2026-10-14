"use client";
// The leaderboard rows. Edge-to-edge table, no Card: dense data reads in rows.
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Table, proportional, pixel } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { UserGroupIcon } from "@heroicons/react/24/outline";
import { Icon } from "@astryxdesign/core/Icon";
import { UserAvatar } from "@/lib/user-avatar";
import { useT, useLocale } from "@/lib/i18n";
import { Trend } from "./Trend";
import { money, num, pct, type RankingData, type RankRow } from "./types";

const MEDALS: Record<number, string> = { 1: "🥇", 2: "🥈", 3: "🥉" };

export function metricLabel(metric: string): string {
  return ({ completion: "Généré", prompt: "Entrée", requests: "Requêtes", per_request: "Tokens / requête" } as
    Record<string, string>)[metric] ?? "Total";
}

type Props = {
  data: RankingData;
  rows: RankRow[];
  /** Opens one account's detail (itself, or any account for an admin). */
  onOpen: (username: string) => void;
};

export function RankingTable({ data, rows, onOpen }: Props) {
  const t = useT();
  const locale = useLocale();
  const isGroup = data.by === "group";
  const perRequest = data.metric === "per_request";

  const name = (r: RankRow) => {
    if (r.username === null) return t(isGroup ? "Groupe masqué" : "Compte anonyme");
    if (r.username === "unknown") return t("Clés non attribuées");
    if (isGroup && r.username === "Sans groupe") return t("Sans groupe");    // the server's label
    return r.username;
  };

  const columns: TableColumn<RankRow>[] = [
    {
      key: "rank", header: t("Rang"), width: pixel(64),
      renderCell: (r) => (
        <Text weight="bold" hasTabularNumbers color={r.is_unattributed ? "secondary" : undefined}>
          {r.rank ? MEDALS[r.rank] ?? String(r.rank) : "—"}
        </Text>
      ),
    },
    {
      key: "username", header: t(isGroup ? "Groupe" : "Compte"), width: proportional(3),
      renderCell: (r) => {
        const canOpen = !isGroup && !r.is_unattributed && r.username !== null && (r.is_me || data.is_admin);
        return (
          <VStack gap={1}>
            <HStack gap={2} vAlign="center">
              {/* No face for unassigned keys, anonymous accounts or groups. */}
              {isGroup ? <Icon icon={UserGroupIcon} size="sm" />
                : !r.is_unattributed && <UserAvatar avatarId={r.avatar_id} username={r.username ?? "anonyme"} size="sm" />}
              {canOpen ? (
                <Button label={name(r)} variant="ghost" size="sm" onClick={() => onOpen(r.username!)} />
              ) : (
                <Text weight={r.is_me ? "bold" : undefined}
                  color={r.is_unattributed || r.username === null ? "secondary" : undefined}>{name(r)}</Text>
              )}
              {r.is_me && <Badge label={t("toi")} variant="neutral" />}
              {isGroup && r.members ? (
                <Text type="supporting" color="secondary">
                  {t(r.members > 1 ? "{n} membres" : "{n} membre").replace("{n}", String(r.members))}
                </Text>
              ) : null}
            </HStack>
            {/* Under 0,5 % a model reads « 0 % »: noise, not information. */}
            {r.models.some((m) => m.share_pct >= 0.5) && !data.model ? (
              <Text type="supporting" color="secondary">
                {r.models.filter((m) => m.share_pct >= 0.5)
                  .map((m) => `${m.model} ${pct(m.share_pct, locale, 0)}`).join(" · ")}
              </Text>
            ) : null}
          </VStack>
        );
      },
    },
  ];
  if (!perRequest) {
    columns.push({
      key: "share_pct", header: t("Part du total"), width: proportional(2),
      renderCell: (r) => (
        <VStack gap={1}>
          <Text hasTabularNumbers size="sm" color={r.is_unattributed ? "secondary" : undefined}>
            {pct(r.share_pct ?? 0, locale)}
          </Text>
          <ProgressBar label={t("Part du total")} isLabelHidden value={Math.min(100, r.share_pct ?? 0)} />
        </VStack>
      ),
    });
  }
  columns.push(
    {
      key: "value", header: t(metricLabel(data.metric)), width: proportional(2), align: "end",
      renderCell: (r) => (
        <VStack gap={0} align="end">
          <Text weight="bold" hasTabularNumbers>{num(r.value, locale)}</Text>
          {/* What explains the figure: the generated part of a total, the
              requests behind an average, the size behind a request count. */}
          <Text type="supporting" color="secondary">
            {data.metric === "total"
              ? t("dont {n} générés").replace("{n}", num(r.completion, locale))
              : perRequest
                ? t("{n} requêtes").replace("{n}", num(r.requests, locale))
                : data.metric === "requests"
                  ? t("{n} tokens").replace("{n}", num(r.tokens, locale))
                  : null}
          </Text>
          <Text type="supporting" color="secondary">≈ {money(r.cost, data.prices.currency, locale)}</Text>
        </VStack>
      ),
    },
    {
      key: "trend", header: t("Tendance"), width: pixel(112),
      renderCell: (r) => <Trend points={r.trend} myself={r.is_me} />,
    },
    {
      key: "delta",
      header: data.has_prev ? t("Évolution vs {p}").replace("{p}", t(data.prev_label)) : t("Évolution"),
      width: pixel(104), align: "end",
      renderCell: (r) =>
        r.delta == null ? (
          <Text type="supporting" color="secondary">—</Text>
        ) : (
          <Text type="supporting" color={r.delta >= 0 ? "accent" : "secondary"} hasTabularNumbers>
            {r.delta >= 0 ? "▲" : "▼"} {pct(Math.abs(r.delta), locale, 0)}
          </Text>
        ),
    },
  );
  if (data.is_admin && !isGroup) {
    columns.push({
      key: "quota_pct", header: t("Quota utilisé"), width: pixel(104), align: "end",
      renderCell: (r) =>
        r.quota_pct == null ? (
          <Text type="supporting" color="secondary">{r.is_unattributed ? "—" : t("illimité")}</Text>
        ) : (
          <VStack gap={1}>
            <Text type="supporting" hasTabularNumbers>{pct(r.quota_pct, locale, 0)}</Text>
            <ProgressBar label={t("Quota utilisé")} isLabelHidden value={Math.min(100, r.quota_pct)}
              variant={r.quota_pct >= 90 ? "warning" : "accent"} />
          </VStack>
        ),
    });
  }
  return (
    <Table data={rows} columns={columns} idKey={(r) => r.username ?? r.anon_id ?? String(r.rank)}
      density="compact" hasHover verticalAlign="middle" />
  );
}
