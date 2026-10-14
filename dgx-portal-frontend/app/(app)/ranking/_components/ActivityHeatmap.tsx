"use client";
// When the box works: successful requests per day of the week × hour (local
// time), for the viewer or the whole platform. Tells when to schedule a
// maintenance or a model switch without cutting anyone off.
//
// One hue, light → dark (sequential): the accent at a growing opacity over
// the card, so both themes follow the tokens. The scale is a square root:
// office hours dwarf the night, and a linear scale left the evenings blank.
import { useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { num, type Activity } from "./types";

const DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"];
const CELL = 22;
const GAP = 2;
const LABEL_W = 34;
const TOP = 16;

function opacity(n: number, max: number) {
  return n <= 0 || max <= 0 ? 0 : 0.18 + 0.82 * Math.sqrt(n / max);
}

export function ActivityHeatmap() {
  const t = useT();
  const locale = useLocale();
  const [scope, setScope] = useState("all");
  const [period, setPeriod] = useState("month");
  const [data, setData] = useState<Activity | null>(null);

  useEffect(() => {
    let alive = true;
    getJSON<Activity>(`/api/ranking/activity?period=${period}&scope=${scope}`)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); });
    return () => { alive = false; };
  }, [scope, period]);

  const W = LABEL_W + 24 * (CELL + GAP);
  const H = TOP + 7 * (CELL + GAP);
  let busiest: { d: number; h: number; n: number } | null = null;
  data?.cells.forEach((row, d) => row.forEach((c, h) => {
    if (c.requests > (busiest?.n ?? 0)) busiest = { d, h, n: c.requests };
  }));
  const peak = busiest as { d: number; h: number; n: number } | null;

  return (
    <Card>
      <VStack gap={3}>
        <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Quand la plateforme travaille")}</Text>
            <Text type="supporting" color="secondary">
              {t("Requêtes réussies par jour et par heure (heure locale).")}
            </Text>
          </VStack>
          <HStack gap={2} wrap="wrap">
            <SegmentedControl label={t("Portée")} value={scope} onChange={setScope} size="sm">
              <SegmentedControlItem value="all" label={t("Plateforme")} />
              <SegmentedControlItem value="me" label={t("Moi")} />
            </SegmentedControl>
            <SegmentedControl label={t("Période de la carte")} value={period} onChange={setPeriod} size="sm">
              <SegmentedControlItem value="week" label={t("7 j")} />
              <SegmentedControlItem value="month" label={t("30 j")} />
              <SegmentedControlItem value="year" label={t("12 mois")} />
            </SegmentedControl>
          </HStack>
        </HStack>
        {data && data.total_requests > 0 ? (
          <VStack gap={2}>
            <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", maxWidth: W * 1.4, height: "auto", display: "block" }}
              role="img" aria-label={t("Carte d'activité : requêtes par jour de la semaine et par heure")}>
              {[0, 6, 12, 18].map((h) => (
                <text key={h} x={LABEL_W + h * (CELL + GAP)} y={11}
                  style={{ fontSize: 10, fill: "var(--color-text-secondary)" }}>{`${h} h`}</text>
              ))}
              {data.cells.map((row, d) => (
                <g key={d}>
                  <text x={0} y={TOP + d * (CELL + GAP) + CELL * 0.7}
                    style={{ fontSize: 10, fill: "var(--color-text-secondary)" }}>{t(DAYS[d]).slice(0, 3)}</text>
                  {row.map((c, h) => (
                    <g key={h}>
                      <rect x={LABEL_W + h * (CELL + GAP)} y={TOP + d * (CELL + GAP)} width={CELL} height={CELL} rx={3}
                        style={{ fill: "var(--color-background-muted)" }} />
                      <rect x={LABEL_W + h * (CELL + GAP)} y={TOP + d * (CELL + GAP)} width={CELL} height={CELL} rx={3}
                        style={{ fill: "var(--color-accent)", fillOpacity: opacity(c.requests, data.max_requests) }}>
                        <title>{t("{d} {h} h : {n} requêtes · {k} tokens")
                          .replace("{d}", t(DAYS[d])).replace("{h}", String(h))
                          .replace("{n}", num(c.requests, locale)).replace("{k}", num(c.tokens, locale))}</title>
                      </rect>
                    </g>
                  ))}
                </g>
              ))}
            </svg>
            <HStack gap={2} vAlign="center" wrap="wrap">
              <Text type="supporting" color="secondary">{t("moins")}</Text>
              <svg viewBox="0 0 76 12" style={{ width: 76, height: 12 }} aria-hidden="true">
                {[0.18, 0.38, 0.58, 0.78, 1].map((o, i) => (
                  <rect key={o} x={i * 15} y={0} width={13} height={12} rx={2}
                    style={{ fill: "var(--color-accent)", fillOpacity: o }} />
                ))}
              </svg>
              <Text type="supporting" color="secondary">{t("plus")}</Text>
              {peak ? (
                <Text type="supporting" color="secondary">
                  {" · "}{t("Créneau le plus chargé : {d} {h} h ({n} requêtes).")
                    .replace("{d}", t(DAYS[peak.d])).replace("{h}", String(peak.h))
                    .replace("{n}", num(peak.n, locale))}
                </Text>
              ) : null}
            </HStack>
          </VStack>
        ) : (
          <Text type="supporting" color="secondary">
            {data ? t("Aucune requête sur cette période.") : t("Chargement…")}
          </Text>
        )}
      </VStack>
    </Card>
  );
}
