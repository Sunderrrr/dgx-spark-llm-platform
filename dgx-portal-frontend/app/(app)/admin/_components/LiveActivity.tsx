"use client";

// « Activité en direct » — the living state of the box: sessions, memory,
// throughput. State only, nothing to act on: the operator opens the admin
// page ten times a day to answer « what is the machine doing right now », and
// until now that answer only lived on the home page.
//
// Two endpoints, chosen for what they cost:
//   - /api/modelhealth (vllm_health, cached 1 s server-side): the fast gauge —
//     throughput, sessions, TTFT. Polled every 2 s, exactly like the dashboard
//     it was made for;
//   - /system/stats (runner metrics + active users): memory (the runner spawns
//     nvidia-smi, cached 3 s server-side) and « qui utilise le modèle ». Polled
//     every 8 s, the cadence of the rest of the admin page.
// No backend change: both routes already existed.
//
// DISPLAY RULE (same as the home page): a missing value is « — », never a 0
// that would read as « nothing to report ». A 0 is only displayed when the
// engine really reported one (a free engine, a counter at rest).

import { useEffect, useState, type ReactNode } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Grid } from "@astryxdesign/core/Grid";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";

type ModelHealth = {
  model: string | null;
  up: boolean;
  tps: number | null;
  running: number;
  waiting: number;
  ttft: number | null;
  requests: number | null;
  max_seqs: number | null;
  tokens_generated?: number | null;
  tps_moyen?: number | null;
  tokens_prompt?: number | null;
  tps_prefill?: number | null;
  // In-flight activity seen by the engine (llama.cpp /slots). Absent for vLLM
  // and for a stopped engine: the absence is « — », never « nobody works ».
  slots?: {
    busy: number;
    total: number;
    plus_ancien_s: number | null;
    prompt_ingere: number;
    prompt_traite: number;
  } | null;
} | null;

type SysMetrics = {
  cpu_pct: number | null;
  ram?: { used_gb: number; total_gb: number; pct: number } | null;
  gpu?: { util: number | null; power: number | null; temp: number | null } | null;
} | null;

type ActiveUser = {
  username: string;
  requests: number;
  tokens: number;
  live?: boolean;
  derniere_s?: number | null;
  en_vol?: boolean;
  depuis_s?: number | null;
};

type SystemStats = {
  cpu_pct?: number | null;
  ram?: { used_gb: number; total_gb: number; pct: number } | null;
  gpu?: { util: number | null; power: number | null; temp: number | null } | null;
  model?: ModelHealth;
  active_users?: ActiveUser[] | null;
};

/** How long since this account last asked the model for anything?
 *  Same vocabulary as the home page (« il y a 4 s » cannot be confused with
 *  « il y a 41 min »); the helper is duplicated rather than moved out of
 *  page.tsx — the home page is out of scope. */
function ageDepuis(secondes: number | null | undefined, t: (s: string) => string): string {
  if (secondes == null) return t("activité inconnue");
  if (secondes < 5) return t("à l'instant");
  if (secondes < 60) return t("il y a {n} s").replace("{n}", String(Math.round(secondes)));
  if (secondes < 3600) return t("il y a {n} min").replace("{n}", String(Math.round(secondes / 60)));
  return t("il y a {n} h").replace("{n}", String(Math.round(secondes / 3600)));
}

/** One metric of the live row: label, big value, optional precision line. */
function Metric({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <VStack gap={0}>
      <Text type="supporting" color="secondary">{label}</Text>
      <Text weight="semibold" hasTabularNumbers>{value}</Text>
      {sub ? <Text type="supporting" color="secondary" hasTabularNumbers>{sub}</Text> : null}
    </VStack>
  );
}

export function LiveActivity() {
  const t = useT();
  const numLocale = useLocale();
  const [health, setHealth] = useState<ModelHealth>(null);
  const [sys, setSys] = useState<SysMetrics>(null);
  const [users, setUsers] = useState<ActiveUser[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      getJSON<ModelHealth>("/api/modelhealth")
        .then((d) => { if (alive) setHealth(d ?? null); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () => {
      getJSON<SystemStats>("/system/stats")
        .then((d) => {
          if (!alive) return;
          setSys(d ? { cpu_pct: d.cpu_pct ?? null, ram: d.ram ?? null, gpu: d.gpu ?? null } : null);
          setUsers(d?.active_users ?? null);
          // The model health comes back with the same shape: it only serves as
          // a stand-in until the 2 s probe answers.
          setHealth((h) => h ?? d?.model ?? null);
        })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 8000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const slots = health?.slots ?? null;
  const ram = sys?.ram ?? null;
  const ramKnown = !!ram && ram.total_gb > 0;
  const gpu = sys?.gpu ?? null;

  // Memory parts: each one is displayed only when measured — an absent sensor
  // (no nvidia-smi on the box) is « — », not a silent 0 W / 0 °C.
  const gpuParts: string[] = [];
  if (gpu?.util != null) gpuParts.push(`${Math.round(gpu.util)} %`);
  if (gpu?.power != null) gpuParts.push(`${Math.round(gpu.power)} W`);
  if (gpu?.temp != null) gpuParts.push(`${Math.round(gpu.temp)} °C`);

  return (
    <Card>
      <VStack gap={4}>
        <HStack gap={2} vAlign="center" hAlign="between" wrap="wrap">
          <Text weight="semibold">{t("Activité en direct")}</Text>
          <Text type="supporting" color="secondary">{t("État vivant du moteur et de la machine — rien à actionner ici.")}</Text>
        </HStack>

        {/* Throughput and sessions, straight from the engine counters. */}
        <HStack gap={5} wrap="wrap">
          <Metric
            label={t("Modèle actif")}
            value={
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{health?.model || t("aucun")}</Text>
                {health && <Badge label={health.up ? t("en ligne") : t("arrêté")} variant={health.up ? "success" : "neutral"} />}
              </HStack>
            }
          />
          <Metric
            label={t("Tokens générés")}
            value={`${health?.tps ?? "—"} tok/s`}
            sub={health?.tps_moyen != null ? t("moyenne {n} tok/s").replace("{n}", String(health.tps_moyen)) : null}
          />
          <Metric
            label={t("Prefill")}
            value={health?.tps_prefill != null ? `${health.tps_prefill} tok/s` : "—"}
            sub={health?.tokens_prompt != null ? t("{n} tokens d'entrée").replace("{n}", health.tokens_prompt.toLocaleString(numLocale)) : null}
          />
          <Metric
            label={t("Sessions")}
            value={`${health?.running ?? "—"} / ${health?.max_seqs ?? "—"}`}
          />
          <Metric label={t("TTFT")} value={health?.ttft != null ? `${health.ttft} s` : "—"} />
          {/* llama.cpp has no request counter: « — », never a fabricated 0. */}
          <Metric label={t("Requêtes servies")} value={health?.requests ?? "—"} />
        </HStack>

        {/* Memory: on the GB10 the GPU eats the very same pool as the RAM, so
            the two are read side by side. */}
        <Grid columns={{ minWidth: 220, max: 3 }} gap={4}>
          <VStack gap={1}>
            <HStack hAlign="between">
              <Text type="supporting" color="secondary">{t("CPU")}</Text>
              <Text hasTabularNumbers>{sys?.cpu_pct != null ? `${sys.cpu_pct} %` : "—"}</Text>
            </HStack>
            {sys?.cpu_pct != null && <ProgressBar label={t("CPU")} isLabelHidden value={sys.cpu_pct} />}
          </VStack>
          <VStack gap={1}>
            <HStack hAlign="between">
              <Text type="supporting" color="secondary">{t("RAM")}</Text>
              <Text hasTabularNumbers>
                {ramKnown ? `${ram!.used_gb} / ${ram!.total_gb} ${t("Go")}` : "—"}
              </Text>
            </HStack>
            {ramKnown && <ProgressBar label={t("RAM")} isLabelHidden value={ram!.pct} />}
          </VStack>
          <VStack gap={1}>
            <HStack hAlign="between">
              <Text type="supporting" color="secondary">{t("GPU")}</Text>
              <Text hasTabularNumbers>{gpuParts.length > 0 ? gpuParts.join(" · ") : "—"}</Text>
            </HStack>
            {gpu?.util != null && <ProgressBar label={t("GPU")} isLabelHidden value={gpu.util} />}
          </VStack>
        </Grid>

        {/* Who is on the model — the admin-only panel of the home page, with
            its ages (« il y a 4 s » versus « il y a 41 min ») and its engine
            sessions, the only description available DURING a request. */}
        <VStack gap={2}>
          <Text type="supporting" color="secondary">{t("Qui utilise le modèle · 30 dernières minutes · visible admin uniquement")}</Text>
          {slots ? (
            <Text type="supporting" color="secondary" hasTabularNumbers>
              {t("{n} session(s) en cours sur le moteur").replace("{n}", String(slots.busy))}
              {/* The total IS the answer to « how many sessions does the engine
                  take »: it is shown even at zero. */}
              {slots.total != null ? ` / ${slots.total}` : ""}
              {slots.plus_ancien_s != null
                ? " · " + t("la plus ancienne depuis {d}").replace("{d}", ageDepuis(slots.plus_ancien_s, t))
                : ""}
              {slots.prompt_ingere > slots.prompt_traite
                ? " · " + t("ingestion du prompt {a} / {b} tokens")
                    .replace("{a}", slots.prompt_traite.toLocaleString(numLocale))
                    .replace("{b}", slots.prompt_ingere.toLocaleString(numLocale))
                : ""}
            </Text>
          ) : (
            <>
              {/* Missing is « — », never a 0 that would read as « nobody is
                  working »: vLLM exposes no /slots, and a stopped engine
                  reports nothing at all. */}
              <Text type="supporting" color="secondary" hasTabularNumbers>—</Text>
              <Text type="supporting" color="secondary">{t("Le moteur ne publie pas l'état de ses sessions.")}</Text>
            </>
          )}
          {users === null ? (
            <Text type="supporting" color="secondary">{t("Relevé indisponible — impossible de lire l'état de la plateforme.")}</Text>
          ) : users.length === 0 ? (
            slots && slots.busy > 0 ? (
              <Text type="supporting" color="secondary">
                {t("Ces requêtes sont anonymes jusqu'à leur fin : LiteLLM n'écrit la ligne qu'une fois la requête terminée.")}
              </Text>
            ) : (
              <Text type="supporting" color="secondary">{t("Personne n'utilise le modèle en ce moment.")}</Text>
            )
          ) : (
            <HStack gap={2} wrap="wrap">
              {users.map((u) => (
                <Badge
                  key={u.username}
                  variant={u.live ? "success" : "neutral"}
                  // The age is what distinguishes « he is generating right now »
                  // from « he finished an hour ago »: a flag alone does not say it.
                  label={[
                    u.username,
                    u.en_vol
                      ? t("en cours depuis {d}").replace("{d}", ageDepuis(u.depuis_s ?? 0, t))
                      : ageDepuis(u.derniere_s, t),
                    u.live ? t("en direct") : null,
                    u.en_vol ? null : `${u.requests} ${t("req")}`,
                    u.en_vol ? null : `${Math.round(u.tokens).toLocaleString(numLocale)} ${t("tok")}`,
                  ].filter(Boolean).join(" · ")}
                />
              ))}
            </HStack>
          )}
          {slots && slots.busy > 0 && (users?.length ?? 0) > 0 && !users!.some((u) => u.live) && (
            <Text type="supporting" color="secondary">
              {t("Aucun de ces comptes n'a émis à l'instant : les sessions en cours ne sont pas encore attribuées.")}
            </Text>
          )}
        </VStack>
      </VStack>
    </Card>
  );
}
