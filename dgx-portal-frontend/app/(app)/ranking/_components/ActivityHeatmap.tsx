"use client";
// When the box works — and a cell is always ONE real moment.
//
// The first version folded every Monday of the period into one « Monday 4 h »
// cell: over 12 months, 52 different moments read as a slot that never
// happened (operator, 2026-10-14). Now:
//   - 7 days: the 7 real dates × the 24 hours (an hour of a given day);
//   - 30 days, 12 months: a calendar, one cell per real day — weeks in
//     columns, weekdays in rows, months named above.
//
// One hue, light → dark (sequential): the accent at a growing opacity, so both
// themes follow the tokens; an empty slot is an outline, never a fill that
// reads like low activity. Square-root scale: office hours dwarf the night.
import { useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { num, type Activity } from "./types";

const WEEKDAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];
const GAP = 2;
const LABEL_W = 62;
const TOP = 16;
const TEXT = { fontSize: 9, fill: "var(--color-text-secondary)" } as const;

function opacity(n: number, max: number) {
  return n <= 0 || max <= 0 ? 0 : 0.18 + 0.82 * Math.sqrt(n / max);
}

const at = (iso: string) => new Date(`${iso}T12:00:00`);

/** An outline (the slot exists) and, over it, the activity fill. */
function Cell({ x, y, size, n, max, title, label }: {
  x: number; y: number; size: number; n: number; max: number; title: string; label?: string;
}) {
  return (
    <g>
      <rect x={x + 0.5} y={y + 0.5} width={size - 1} height={size - 1} rx={3}
        style={{ fill: "none", stroke: "var(--color-border)", strokeWidth: 1 }} />
      <rect x={x} y={y} width={size} height={size} rx={3}
        style={{ fill: "var(--color-accent)", fillOpacity: opacity(n, max) }}>
        <title>{title}</title>
      </rect>
      {label ? (
        // A halo in the card's colour keeps the day number readable on the
        // darkest fills too (it vanished on them, 2026-10-14).
        <text x={x + 4} y={y + 11} style={{
          ...TEXT, fill: "var(--color-text-primary)", stroke: "var(--color-background-surface)",
          strokeWidth: 2.5, paintOrder: "stroke", pointerEvents: "none",
        }}>{label}</text>
      ) : null}
    </g>
  );
}

export function ActivityHeatmap() {
  const t = useT();
  const locale = useLocale();
  const [scope, setScope] = useState("all");
  const [period, setPeriod] = useState("week");
  const [data, setData] = useState<Activity | null>(null);

  useEffect(() => {
    let alive = true;
    getJSON<Activity>(`/api/ranking/activity?period=${period}&scope=${scope}`)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); });
    return () => { alive = false; };
  }, [scope, period]);

  const fullDate = (iso: string) =>
    at(iso).toLocaleDateString(locale, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const shortDate = (iso: string) => at(iso).toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "short" });
  const slot = (n: number, k: number) =>
    t("{n} requêtes · {k} tokens").replace("{n}", num(n, locale)).replace("{k}", num(k, locale));

  let chart: React.ReactNode = null;
  let summary = "";
  if (data?.mode === "hours" && data.rows) {
    const CELL = 22;
    const W = LABEL_W + 24 * (CELL + GAP);
    const H = TOP + data.rows.length * (CELL + GAP);
    let peak = { day: "", h: 0, n: 0 };
    data.rows.forEach((r) => r.cells.forEach((c, h) => { if (c.requests > peak.n) peak = { day: r.day, h, n: c.requests }; }));
    if (peak.n) {
      summary = t("Créneau le plus chargé : {d} {h} h ({n} requêtes).")
        .replace("{d}", shortDate(peak.day)).replace("{h}", String(peak.h)).replace("{n}", num(peak.n, locale));
    }
    chart = (
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", maxWidth: W * 1.4, height: "auto", display: "block" }}
        role="img" aria-label={t("Requêtes par heure, sur chacun des 7 derniers jours")}>
        {Array.from({ length: 24 }, (_, h) => (
          <text key={h} x={LABEL_W + h * (CELL + GAP) + CELL / 2} y={11} textAnchor="middle" style={TEXT}>{h}</text>
        ))}
        {data.rows.map((r, d) => (
          <g key={r.day}>
            <text x={0} y={TOP + d * (CELL + GAP) + CELL * 0.68} style={{ ...TEXT, fontSize: 10 }}>{shortDate(r.day)}</text>
            {r.cells.map((c, h) => (
              <Cell key={h} x={LABEL_W + h * (CELL + GAP)} y={TOP + d * (CELL + GAP)} size={CELL}
                n={c.requests} max={data.max_requests}
                title={`${fullDate(r.day)}, ${h} h : ${slot(c.requests, c.tokens)}`} />
            ))}
          </g>
        ))}
      </svg>
    );
  } else if (data?.mode === "days" && data.days) {
    // Calendar: the first column starts on the Monday of the first day's week.
    const big = data.days.length <= 31;
    const CELL = big ? 30 : 12;
    const first = (at(data.days[0].day).getDay() + 6) % 7;
    const cols = Math.ceil((first + data.days.length) / 7);
    const W = 30 + cols * (CELL + GAP);
    const H = TOP + 7 * (CELL + GAP);
    let peak = { day: "", n: 0 };
    data.days.forEach((x) => { if (x.requests > peak.n) peak = { day: x.day, n: x.requests }; });
    if (peak.n) {
      summary = t("Jour le plus chargé : {d} ({n} requêtes).")
        .replace("{d}", fullDate(peak.day)).replace("{n}", num(peak.n, locale));
    }
    const months: { col: number; label: string }[] = [];
    data.days.forEach((x, i) => {
      const d = at(x.day);
      if (i === 0 || d.getDate() === 1) {
        months.push({ col: Math.floor((first + i) / 7), label: d.toLocaleDateString(locale, { month: "short" }) });
      }
    });
    chart = (
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", maxWidth: big ? W * 1.6 : W * 1.3, height: "auto", display: "block" }}
        role="img" aria-label={t("Requêtes par jour, en calendrier")}>
        {months.map((m, i) => (
          // A month name needs ~26 px: when the next one starts closer (a
          // short first month), the first gives way rather than overlap.
          i + 1 < months.length && (months[i + 1].col - m.col) * (CELL + GAP) < 26 ? null : (
            <text key={`${m.col}-${m.label}`} x={30 + m.col * (CELL + GAP)} y={11} style={TEXT}>{m.label}</text>
          )
        ))}
        {WEEKDAYS.map((w, r) => (r % 2 === 0 || big ? (
          <text key={w} x={0} y={TOP + r * (CELL + GAP) + CELL * 0.72} style={TEXT}>{t(w)}</text>
        ) : null))}
        {data.days.map((x, i) => {
          const col = Math.floor((first + i) / 7);
          const row = (first + i) % 7;
          return (
            <Cell key={x.day} x={30 + col * (CELL + GAP)} y={TOP + row * (CELL + GAP)} size={CELL}
              n={x.requests} max={data.max_requests} label={big ? String(at(x.day).getDate()) : undefined}
              title={`${fullDate(x.day)} : ${slot(x.requests, x.tokens)}`} />
          );
        })}
      </svg>
    );
  }

  return (
    <Card>
      <VStack gap={3}>
        <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Quand la plateforme travaille")}</Text>
            <Text type="supporting" color="secondary">
              {period === "week"
                ? t("Requêtes réussies, heure par heure sur chacun des 7 derniers jours (heure locale).")
                : t("Requêtes réussies, jour par jour.")}
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
        {data && data.total_requests > 0 && chart ? (
          <VStack gap={2}>
            {chart}
            <HStack gap={2} vAlign="center" wrap="wrap">
              <Text type="supporting" color="secondary">{t("moins")}</Text>
              <svg viewBox="0 0 76 12" style={{ width: 76, height: 12 }} aria-hidden="true">
                {[0.18, 0.38, 0.58, 0.78, 1].map((o, i) => (
                  <rect key={o} x={i * 15} y={0} width={13} height={12} rx={2}
                    style={{ fill: "var(--color-accent)", fillOpacity: o }} />
                ))}
              </svg>
              <Text type="supporting" color="secondary">{t("plus")}</Text>
              {summary ? <Text type="supporting" color="secondary">{" · "}{summary}</Text> : null}
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
