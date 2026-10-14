"use client";

import { useEffect, useRef, useState } from "react";
import { useSeed, type Seed } from "@/lib/seed";
import { RANKING_DEFAULT } from "./defaults";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { Switch } from "@astryxdesign/core/Switch";
import { Link } from "@astryxdesign/core/Link";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { ChartBarIcon } from "@heroicons/react/24/outline";
import { getJSON, postForm } from "@/lib/api";
import { useCsrf } from "@/lib/useCsrf";
import { useT, useLocale } from "@/lib/i18n";
import { RankingTable, metricLabel } from "./_components/RankingTable";
import { ActivityHeatmap } from "./_components/ActivityHeatmap";
import { RecordsCard } from "./_components/RecordsCard";
import { UserDetailDialog } from "./_components/UserDetailDialog";
import { money, num, pct, type RankingData } from "./_components/types";

/* The ranking is about ACTUALLY consumed tokens (input + generated), not
 * about weighted cost, which underestimates prompt-heavy loads by a factor
 * of ~10. The readings do not crown the same winner — measurement of
 * 2026-09-14 over 30 days: #1 concentrated 30.9 % of the total but 0.46 % of
 * the generated — hence the metric selector, which since 2026-10-14 also
 * counts requests and their average size. Every choice goes back to the
 * server, which re-sorts and recomputes: the screen never re-sorts a ranking
 * itself. */

const PERIODS = [
  { value: "day", label: "Jour" },
  { value: "week", label: "Semaine" },
  { value: "month", label: "Mois" },
  { value: "year", label: "Année" },
  { value: "all", label: "Tout" },
];

const METRICS = ["total", "completion", "prompt", "requests", "per_request"];
const ALL_MODELS = "__all__";

export type RankingSeed = { ranking: RankingData };

export default function RankingPage({ seed }: { seed?: Seed<{ ranking: RankingData | null }> | null }) {
  const { data: initial, fresh } = useSeed<RankingSeed>(seed);
  const t = useT();
  const locale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [period, setPeriod] = useState<string>(RANKING_DEFAULT.period);
  const [metric, setMetric] = useState<string>(RANKING_DEFAULT.metric);
  const [by, setBy] = useState<string>("account");
  const [model, setModel] = useState<string>(ALL_MODELS);
  const [detail, setDetail] = useState<string | null>(null);
  const [state, setState] = useState<{ data: RankingData | null; failed: boolean }>({
    data: initial.ranking ?? null,
    failed: false,
  });
  // The default view came with the page: its first run skips the fetch.
  const servedView = useRef(fresh.ranking);
  const query = new URLSearchParams({ period, metric, by, ...(model !== ALL_MODELS ? { model } : {}) }).toString();

  useEffect(() => {
    // An outage must be distinguishable from an absence of consumption, and
    // the `alive` flag discards the answer of a view already left.
    if (servedView.current) {
      servedView.current = false;
      return;
    }
    let alive = true;
    getJSON<RankingData>(`/api/ranking?${query}`)
      .then((d) => { if (alive) setState({ data: d, failed: false }); })
      .catch(() => { if (alive) setState({ data: null, failed: true }); });
    return () => { alive = false; };
  }, [query]);

  const { data, failed } = state;
  const rows = data?.rows ?? [];
  const ranked = rows.filter((r) => !r.is_unattributed);
  const unattributed = rows.filter((r) => r.is_unattributed);
  const me = rows.find((r) => r.is_me && !r.is_group);
  const hasRows = ranked.length > 0;
  const label = t(metricLabel(metric));
  const currency = data?.prices.currency ?? "USD";

  async function setAnonymous(v: boolean) {
    const r = await postForm("/api/ranking/privacy", csrf, { anonymous: v ? "1" : "0" }).catch(() => null);
    if (!r?.ok) {
      showToast({ body: t("Échec de l'enregistrement"), type: "error" });
      return;
    }
    setState((s) => (s.data ? { ...s, data: { ...s.data, me_anonymous: v } } : s));
    showToast({ body: v ? t("Tu apparais désormais anonymement aux autres.") : t("Ton nom est de nouveau visible."), type: "info" });
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5} maxWidth={1100}>
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

            {/* Personal position: the only piece of information one came for. */}
            {data && (
              <Card>
                <VStack gap={3}>
                  {me ? (
                    <HStack gap={5} vAlign="center" wrap="wrap">
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{t("Votre position")}</Text>
                        <Text weight="bold" size="lg" hasTabularNumbers>{me.rank ? `#${me.rank}` : "—"}</Text>
                      </VStack>
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{label}</Text>
                        <Text weight="bold" hasTabularNumbers>{num(me.value, locale)}</Text>
                      </VStack>
                      {me.share_pct != null && (
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Part du total")}</Text>
                          <Text weight="bold" hasTabularNumbers>{pct(me.share_pct, locale)}</Text>
                        </VStack>
                      )}
                      {data.has_prev && (
                        <VStack gap={0}>
                          <Text type="supporting" color="secondary">{t("Évolution")}</Text>
                          <Text weight="bold" hasTabularNumbers>
                            {me.delta == null ? "—" : `${me.delta >= 0 ? "▲" : "▼"} ${pct(Math.abs(me.delta), locale, 0)}`}
                          </Text>
                        </VStack>
                      )}
                      <VStack gap={0}>
                        <Text type="supporting" color="secondary">{t("Ce que ça aurait coûté")}</Text>
                        <Text weight="bold" hasTabularNumbers>{money(me.cost, currency, locale)}</Text>
                      </VStack>
                      <Text type="supporting" color="secondary">
                        {t("sur {n} comptes actifs").replace("{n}", String(data.active_count))}
                      </Text>
                    </HStack>
                  ) : (
                    <Text type="supporting" color="secondary">{t("Vous n'avez rien consommé sur cette période.")}</Text>
                  )}
                  <HStack gap={2} vAlign="center" wrap="wrap">
                    <Switch label={t("Apparaître anonymement aux autres")} value={data.me_anonymous}
                      onChange={(v) => void setAnonymous(v)} />
                    <Text type="supporting" color="secondary">
                      {t("Ton nom et ton avatar sont masqués dans le classement et les records ; tu gardes ta position.")}
                    </Text>
                  </HStack>
                </VStack>
              </Card>
            )}

            <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
              <HStack gap={2} vAlign="center" wrap="wrap">
                <SegmentedControl label={t("Métrique")} value={metric} onChange={setMetric}>
                  {METRICS.map((m) => <SegmentedControlItem key={m} value={m} label={t(metricLabel(m))} />)}
                </SegmentedControl>
                <SegmentedControl label={t("Classer")} value={by} onChange={setBy}>
                  <SegmentedControlItem value="account" label={t("Comptes")} />
                  <SegmentedControlItem value="group" label={t("Groupes")} />
                </SegmentedControl>
                {data && data.models.length > 1 && (
                  <Selector label={t("Modèle")} isLabelHidden size="sm" value={model}
                    onChange={(v) => setModel(v ?? ALL_MODELS)}
                    options={[{ value: ALL_MODELS, label: t("Tous les modèles") },
                      ...data.models.map((m) => ({ value: m.model, label: m.model }))]} />
                )}
              </HStack>
              {data?.is_admin && hasRows && (
                <Link href={`/api/ranking/export.csv?${query}`}>{t("Exporter en CSV")}</Link>
              )}
            </HStack>
            {data && hasRows && (
              <Text type="supporting" color="secondary">
                {t(data.period_label)} · {data.active_count} {t(data.by === "group"
                  ? (data.active_count > 1 ? "groupes actifs" : "groupe actif")
                  : (data.active_count > 1 ? "comptes actifs" : "compte actif"))}
                {" · "}{num(data.total, locale)} {metric === "per_request" ? t("tokens par requête sur la plateforme")
                  : metric === "requests" ? t("requêtes au total") : t("tokens au total")}
                {metric !== "per_request" && <>{" · "}{num(data.avg, locale)} {t("en moyenne")}</>}
                {" · "}{t("≈ {v} au tarif de référence").replace("{v}", money(data.cost_total, currency, locale))}
              </Text>
            )}

            {failed && (
              <EmptyState icon={<Icon icon={ChartBarIcon} size="lg" />} title={t("Classement indisponible.")}
                description={t("La consommation n'a pas pu être lue. Réessaie dans un instant.")} />
            )}
            {!failed && data && !hasRows && (
              <EmptyState icon={<Icon icon={ChartBarIcon} size="lg" />} title={t("Aucune consommation sur cette période.")} />
            )}
            {!failed && data && hasRows && (
              <RankingTable data={data} rows={[...ranked, ...unattributed]} onOpen={setDetail} />
            )}

            {!failed && data && hasRows && (
              <VStack gap={1}>
                <Text type="supporting" color="secondary">
                  {t("Total = entrée + généré.")}
                  {!data.has_prev && ` ${t("« Depuis le début » n'a pas de période précédente : aucune évolution à afficher.")}`}
                </Text>
                <Text type="supporting" color="secondary">
                  {t("Coût de référence : ce que la consommation aurait coûté sur une API commerciale à {i} (entrée) et {o} (sortie) par million de tokens. Une indication, pas une facture.")
                    .replace("{i}", money(data.prices.input, currency, locale))
                    .replace("{o}", money(data.prices.output, currency, locale))}
                </Text>
                {unattributed.length > 0 && (
                  <Text type="supporting" color="secondary">
                    {t("Les lignes non attribuées ne correspondent à aucun compte (clés supprimées ou de test) : elles consomment sans être classées.")}
                  </Text>
                )}
              </VStack>
            )}

            <ActivityHeatmap />
            <RecordsCard />
            <UserDetailDialog username={detail} onClose={() => setDetail(null)} />
          </VStack>
        </LayoutContent>
      }
    />
  );
}
