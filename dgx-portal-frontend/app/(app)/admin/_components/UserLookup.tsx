"use client";

import { useMemo, useState, type ReactNode } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { useT, useLang } from "@/lib/i18n";

// Tout est déjà chargé côté admin : on ne fait PAS de nouvel appel réseau, on
// agrège par utilisateur ce que la page a reçu. But : remplacer les tableaux qui
// déballent tout le monde par une recherche ciblée « un utilisateur à la fois ».
export type SpendRow = { username: string; tokens: number; max_budget: number | null; unlimited: boolean; key_count: number };
export type UsageRow = { username: string; c: number; last: string };
export type ReqRow = { username: string; fullname?: string; model_id: string; status: string; created_at: string };

type Profile = {
  username: string;
  fullname: string | null;
  spend: SpendRow | null;
  ocr: UsageRow | null;
  video: UsageRow | null;
  voice: UsageRow | null;
  requests: ReqRow[];
};

const REQ_VARIANT: Record<string, "warning" | "success" | "error"> = { pending: "warning", done: "success", rejected: "error" };
const REQ_LABEL: Record<string, string> = { pending: "En attente", done: "Lancé ✓", rejected: "Refusé" };

export function UserLookup({
  spend, ocr, video, voice, requests,
}: {
  spend: SpendRow[]; ocr: UsageRow[]; video: UsageRow[]; voice: UsageRow[]; requests: ReqRow[];
}) {
  const t = useT();
  const { lang } = useLang();
  const numLocale = lang === "fr" ? "fr-FR" : "en-US";
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);

  // Index par utilisateur (union de toutes les sources).
  const profiles = useMemo(() => {
    const byUser = new Map<string, Profile>();
    const ensure = (u: string) =>
      byUser.get(u) ?? byUser.set(u, { username: u, fullname: null, spend: null, ocr: null, video: null, voice: null, requests: [] }).get(u)!;
    spend.forEach((r) => { ensure(r.username).spend = r; });
    ocr.forEach((r) => { ensure(r.username).ocr = r; });
    video.forEach((r) => { ensure(r.username).video = r; });
    voice.forEach((r) => { ensure(r.username).voice = r; });
    requests.forEach((r) => {
      const p = ensure(r.username);
      p.requests.push(r);
      if (r.fullname && !p.fullname) p.fullname = r.fullname;
    });
    return byUser;
  }, [spend, ocr, video, voice, requests]);

  const allNames = useMemo(() => Array.from(profiles.keys()).sort((a, b) => a.localeCompare(b)), [profiles]);
  const q = query.trim().toLowerCase();
  const matches = q ? allNames.filter((n) => n.toLowerCase().includes(q)).slice(0, 8) : [];
  const current = picked ? profiles.get(picked) ?? null : null;

  const fmt = (n: number) => Math.round(n).toLocaleString(numLocale);
  const fmtDate = (s: string) => s.slice(0, 16).replace("T", " ");

  function metric(label: string, value: ReactNode, sub?: string | null) {
    return (
      <VStack gap={0}>
        <Text type="supporting" color="secondary">{label}</Text>
        <Text weight="semibold" hasTabularNumbers>{value}</Text>
        {sub ? <Text type="supporting" color="secondary">{sub}</Text> : null}
      </VStack>
    );
  }

  function usageMetric(label: string, unit: string, row: UsageRow | null) {
    return metric(
      label,
      row ? `${fmt(row.c)} ${unit}` : t("Aucune"),
      row ? `${t("Dernière utilisation")} : ${fmtDate(row.last)}` : null,
    );
  }

  return (
    <VStack gap={3}>
      <VStack gap={1}>
        <Text weight="semibold">{t("Recherche par utilisateur")}</Text>
        <Text type="supporting" color="secondary">
          {t("Cherche un utilisateur pour voir ses quotas et son utilisation (LiteLLM, OCR, vidéo, voix). Réservé aux admins.")}
        </Text>
      </VStack>

      <Card>
        <VStack gap={2}>
          <TextInput
            label={t("Identifiant à rechercher")}
            value={query}
            onChange={(v) => { setQuery(v); setPicked(null); }}
            placeholder="mboitel"
          />
          {q && matches.length === 0 && (
            <Text type="supporting" color="secondary">{t("Aucun utilisateur ne correspond.")}</Text>
          )}
          {matches.length > 0 && !current && (
            <HStack gap={1} wrap="wrap">
              {matches.map((n) => (
                <Button key={n} label={n} variant="ghost" size="sm" onClick={() => setPicked(n)} />
              ))}
            </HStack>
          )}
          {!q && (
            <Text type="supporting" color="secondary">
              {t("Tape un identifiant pour afficher son profil.")} {t("Utilisateurs connus")} : {allNames.length}
            </Text>
          )}
        </VStack>
      </Card>

      {current && (
        <Card>
          <VStack gap={3}>
            <HStack hAlign="between" vAlign="center">
              <VStack gap={0}>
                <Text weight="semibold">{current.username}</Text>
                {current.fullname ? <Text type="supporting" color="secondary">{current.fullname}</Text> : null}
              </VStack>
              <Button label={t("Fermer")} variant="ghost" size="sm" onClick={() => setPicked(null)} />
            </HStack>

            <HStack gap={4} wrap="wrap">
              {metric(
                t("Quota LiteLLM"),
                current.spend
                  ? (current.spend.unlimited ? t("Illimité") : current.spend.max_budget != null ? fmt(current.spend.max_budget) : "—")
                  : t("Aucune clé API"),
                current.spend ? `${t("Consommé aujourd'hui")} : ${fmt(current.spend.tokens)} · ${current.spend.key_count} ${t("clé(s)")}` : null,
              )}
              {usageMetric(t("OCR"), t("extractions"), current.ocr)}
              {usageMetric(t("Vidéo"), t("générations"), current.video)}
              {usageMetric(t("Voix"), t("générations"), current.voice)}
            </HStack>

            {current.requests.length > 0 && (
              <VStack gap={1}>
                <Text type="supporting" color="secondary">{t("Demandes de modèles")}</Text>
                <VStack gap={1}>
                  {current.requests.map((r, i) => (
                    <HStack key={`${r.model_id}-${i}`} gap={2} vAlign="center" hAlign="between" wrap="wrap">
                      <Text type="supporting">{r.model_id}</Text>
                      <HStack gap={2} vAlign="center">
                        <Text type="supporting" color="secondary">{fmtDate(r.created_at)}</Text>
                        <Badge label={t(REQ_LABEL[r.status] ?? r.status)} variant={REQ_VARIANT[r.status] ?? "neutral"} />
                      </HStack>
                    </HStack>
                  ))}
                </VStack>
              </VStack>
            )}

            {!current.spend && !current.ocr && !current.video && !current.voice && current.requests.length === 0 && (
              <Text type="supporting" color="secondary">{t("Aucune activité enregistrée pour cet utilisateur.")}</Text>
            )}
          </VStack>
        </Card>
      )}
    </VStack>
  );
}
