"use client";
// Records and streaks, over all time: the biggest days, the longest and the
// running streaks of active days, and who opened each model first. The
// viewer's own records come first — the line one came for.
import { useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Text } from "@astryxdesign/core/Text";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { num, type Records } from "./types";

export function RecordsCard() {
  const t = useT();
  const locale = useLocale();
  const [data, setData] = useState<Records | null>(null);

  useEffect(() => {
    let alive = true;
    getJSON<Records>("/api/ranking/records").then((d) => { if (alive) setData(d); }).catch(() => null);
    return () => { alive = false; };
  }, []);
  if (!data || (!data.best_days.length && !data.firsts.length)) return null;

  const who = (u: string | null, me: boolean) => (me ? t("toi") : u ?? t("Compte anonyme"));
  const day = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" });
  const days = (n: number) => t(n > 1 ? "{n} jours" : "{n} jour").replace("{n}", String(n));

  const section = (title: string, lines: string[]) => (
    <VStack gap={1}>
      <Text weight="semibold" type="supporting">{title}</Text>
      {lines.length ? lines.map((l, i) => <Text key={i} type="supporting">{l}</Text>)
        : <Text type="supporting" color="secondary">—</Text>}
    </VStack>
  );

  return (
    <Card>
      <VStack gap={3}>
        <VStack gap={0}>
          <Text weight="semibold">{t("Records et séries")}</Text>
          <Text type="supporting" color="secondary">{t("Depuis le début, requêtes réussies uniquement.")}</Text>
        </VStack>
        {data.me ? (
          <HStack gap={4} wrap="wrap">
            <Text type="supporting">
              {t("Ton plus gros jour : {d} ({n} tokens)").replace("{d}", day(data.me.best_day.day))
                .replace("{n}", num(data.me.best_day.tokens, locale))}
            </Text>
            <Text type="supporting">
              {t("Série en cours : {s} · record : {r}").replace("{s}", days(data.me.current_streak))
                .replace("{r}", days(data.me.longest_streak))}
            </Text>
            <Text type="supporting">{t("{n} jours actifs").replace("{n}", num(data.me.active_days, locale))}</Text>
            {data.me.first_on.length ? (
              <Text type="supporting">{t("Premier sur : {m}").replace("{m}", data.me.first_on.join(", "))}</Text>
            ) : null}
          </HStack>
        ) : null}
        <Grid columns={{ minWidth: 200, max: 4 }} gap={4}>
          {section(t("Plus gros jours"), data.best_days.map((r) =>
            `${who(r.username, r.is_me)} — ${num(r.tokens, locale)} tokens, ${day(r.day)}`))}
          {section(t("Plus longues séries"), data.longest_streaks.map((r) => `${who(r.username, r.is_me)} — ${days(r.days)}`))}
          {section(t("Séries en cours"), data.current_streaks.map((r) => `${who(r.username, r.is_me)} — ${days(r.days)}`))}
          {section(t("Premiers sur un modèle"), data.firsts.slice(0, 5).map((r) =>
            `${r.model} — ${who(r.username, r.is_me)}, ${day(r.day)}`))}
        </Grid>
      </VStack>
    </Card>
  );
}
