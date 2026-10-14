"use client";
// One account in detail (the account itself, or an admin): what it used,
// per model, and its last 30 days.
import { useEffect, useState } from "react";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Table, proportional } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";
import { Trend } from "./Trend";
import { money, num, type UserDetail } from "./types";

type ModelRow = UserDetail["models"][number] & Record<string, unknown>;

export function UserDetailDialog({ username, onClose }: { username: string | null; onClose: () => void }) {
  const t = useT();
  const locale = useLocale();
  const [data, setData] = useState<UserDetail | null>(null);

  useEffect(() => {
    if (!username) return;
    let alive = true;
    getJSON<UserDetail>(`/api/ranking/user/${encodeURIComponent(username)}?period=month`)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); });
    return () => { alive = false; setData(null); };
  }, [username]);

  const columns: TableColumn<ModelRow>[] = [
    { key: "model", header: t("Modèle"), width: proportional(3) },
    { key: "requests", header: t("Requêtes"), width: proportional(1), align: "end",
      renderCell: (r) => <Text hasTabularNumbers>{num(r.requests, locale)}</Text> },
    { key: "prompt", header: t("Entrée"), width: proportional(2), align: "end",
      renderCell: (r) => <Text hasTabularNumbers>{num(r.prompt, locale)}</Text> },
    { key: "completion", header: t("Généré"), width: proportional(2), align: "end",
      renderCell: (r) => <Text hasTabularNumbers>{num(r.completion, locale)}</Text> },
  ];

  return (
    <Dialog isOpen={username != null} onOpenChange={(o) => { if (!o) onClose(); }} width={640}>
      <DialogHeader title={username ?? ""} subtitle={t("30 derniers jours")} hasDivider
        onOpenChange={(o) => { if (!o) onClose(); }} />
      <VStack padding={3} gap={4}>
        {!data ? (
          <Text type="supporting" color="secondary">{t("Chargement…")}</Text>
        ) : data.requests === 0 ? (
          <Text type="supporting" color="secondary">{t("Aucune requête sur cette période.")}</Text>
        ) : (
          <>
            <HStack gap={5} wrap="wrap">
              {([["Requêtes", num(data.requests, locale)], ["Entrée", num(data.prompt, locale)],
                ["Généré", num(data.completion, locale)],
                ["Coût de référence", money(data.cost, data.currency ?? "USD", locale)]] as const).map(([k, v]) => (
                <VStack key={k} gap={0}>
                  <Text type="supporting" color="secondary">{t(k)}</Text>
                  <Text weight="bold" hasTabularNumbers>{v}</Text>
                </VStack>
              ))}
            </HStack>
            {data.daily.length ? (
              <VStack gap={1}>
                <Text type="supporting" color="secondary">{t("Tokens par jour")}</Text>
                <Trend points={data.daily.map((d) => d.tokens)} myself />
              </VStack>
            ) : null}
            <Table<ModelRow> data={data.models as ModelRow[]} columns={columns} idKey="model" density="compact" />
          </>
        )}
      </VStack>
    </Dialog>
  );
}
