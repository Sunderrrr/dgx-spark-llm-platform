"use client";
// Storage: where generated files live (volume or S3) and where the backups go.
//
// « que l'on puisse choisir que les backups aillent sur le S3, la rétention
// sur l'interface, et que l'archivage … soit en local soit sur le S3 »
// (operator, 2026-10-14). The S3 connection is the « Stockage S3 » card of
// this tab; this card uses it. Conversations stay in the portal database —
// they travel with the backups — so only the files have a location.
//
// Switching the location only changes where NEW files go: existing ones stay
// readable where they are, and « Migrer » moves them in the background.

import { useCallback, useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Banner } from "@astryxdesign/core/Banner";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Table, proportional } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { useLocale, useT, tServer } from "@/lib/i18n";

type Usage = Record<string, { files: number; bytes: number }>;
type Migration = {
  running: boolean; target?: string; total?: number; done?: number; failed?: number;
  bytes?: number; finished?: number | null; last_error?: string;
};
type LastBackup = {
  last_backup?: string; last_attempt?: string; last_ok?: boolean; error?: string;
  file?: string; size?: string; destination?: string; local_count?: number; s3_count?: number | null;
};
type StorageState = {
  s3_ready: boolean;
  media_storage: "local" | "s3";
  media_usage: { local: Usage; s3: Usage | null };
  migration: Migration;
  backup: { destination: "local" | "s3" | "both"; keep_local: number; keep_s3: number };
  last_backup: LastBackup | null;
  max_keep: number;
};

const KIND_LABELS: Record<string, string> = {
  image_files: "Images",
  video_files: "Vidéos",
  ocr_images: "Scans OCR",
  voice_audio: "Audio (voix)",
};

type UsageRow = { kind: string; local: string; s3: string } & Record<string, unknown>;

function bytes(n: number, locale: string): string {
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = n;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u += 1; }
  return `${v.toLocaleString(locale, { maximumFractionDigits: u ? 1 : 0 })} ${units[u]}`;
}

export function StorageConfig({ version }: { version: number }) {
  const t = useT();
  const locale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [state, setState] = useState<StorageState | null>(null);
  const [media, setMedia] = useState<"local" | "s3">("local");
  const [dest, setDest] = useState<"local" | "s3" | "both">("local");
  const [keepLocal, setKeepLocal] = useState(7);
  const [keepS3, setKeepS3] = useState(7);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const d = await fetch("/admin/storage").then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (!d?.ok) return;
    setState(d);
    setMedia(d.media_storage);
    setDest(d.backup.destination);
    setKeepLocal(d.backup.keep_local);
    setKeepS3(d.backup.keep_s3);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads the card, again when the S3 card changes
    void load();
  }, [load, version]);

  // A running migration is followed until it ends.
  const running = !!state?.migration?.running;
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => void load(), 2000);
    return () => clearInterval(id);
  }, [running, load]);

  if (!state) return null;

  const dirty = media !== state.media_storage || dest !== state.backup.destination
    || keepLocal !== state.backup.keep_local || keepS3 !== state.backup.keep_s3;

  async function post(url: string, fields: Record<string, string>) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(fields).toString(),
    });
    return r.json().catch(() => ({}));
  }

  async function save() {
    setSaving(true);
    try {
      const d = await post("/admin/storage", {
        media_storage: media, backup_destination: dest,
        backup_keep_local: String(keepLocal), backup_keep_s3: String(keepS3),
      });
      showToast({ body: d?.ok ? t("Stockage : enregistré.") : tServer(d?.error || t("Échec de l'enregistrement"), t),
        type: d?.ok ? "info" : "error" });
      if (d?.ok) await load();
    } finally {
      setSaving(false);
    }
  }

  async function migrate(target: "local" | "s3") {
    const d = await post("/admin/storage/migrate", { target });
    if (!d?.ok) showToast({ body: tServer(d?.error || t("La migration n'a pas pu démarrer."), t), type: "error" });
    await load();
  }

  const s3 = state.media_usage.s3;
  const rows: UsageRow[] = Object.keys(KIND_LABELS).map((kind) => {
    const l = state.media_usage.local[kind] ?? { files: 0, bytes: 0 };
    const r = s3?.[kind];
    return {
      kind: t(KIND_LABELS[kind]),
      local: `${l.files} · ${bytes(l.bytes, locale)}`,
      s3: r ? `${r.files} · ${bytes(r.bytes, locale)}` : "—",
    };
  });
  const columns: TableColumn<UsageRow>[] = [
    { key: "kind", header: t("Fichiers"), width: proportional(2) },
    { key: "local", header: t("Sur le volume"), width: proportional(2) },
    { key: "s3", header: t("Sur S3"), width: proportional(2) },
  ];
  const localFiles = Object.values(state.media_usage.local).reduce((a, u) => a + u.files, 0);
  const s3Files = s3 ? Object.values(s3).reduce((a, u) => a + u.files, 0) : 0;
  // What is still on the « wrong » side of the chosen location.
  const toMove = state.media_storage === "s3" ? localFiles : s3Files;
  const mig = state.migration;
  const last = state.last_backup;
  const s3Hint = t("Configure d'abord le stockage S3 ci-dessus.");

  return (
    <Card>
      <VStack gap={4}>
        <VStack gap={0}>
          <Text weight="semibold">{t("Stockage et sauvegardes")}</Text>
          <Text type="supporting" color="secondary">
            {t("Où vont les fichiers générés et les sauvegardes. Les conversations restent dans la base du portail, incluse dans chaque sauvegarde.")}
          </Text>
        </VStack>

        <VStack gap={2}>
          <Text weight="semibold" type="supporting">{t("Fichiers générés (images, vidéos, OCR, audio)")}</Text>
          <SegmentedControl label={t("Emplacement des fichiers")} value={media}
            onChange={(v) => setMedia(v as "local" | "s3")}
            isDisabled={!state.s3_ready} disabledMessage={s3Hint}>
            <SegmentedControlItem value="local" label={t("Volume local")} />
            <SegmentedControlItem value="s3" label={t("S3")} />
          </SegmentedControl>
          <Text type="supporting" color="secondary">
            {t("Change l'emplacement des NOUVEAUX fichiers. Les existants restent lisibles là où ils sont ; « Migrer » les déplace.")}
          </Text>
          <Table<UsageRow> data={rows} columns={columns} idKey="kind" density="compact" dividers="rows" />
          {mig?.running ? (
            <VStack gap={1}>
              <ProgressBar label={t("Migration vers {t}").replace("{t}", mig.target === "s3" ? "S3" : t("le volume local"))}
                value={mig.done ?? 0} max={Math.max(1, mig.total ?? 1)} hasValueLabel
                formatValueLabel={(v, m) => `${v} / ${m}`} />
              {mig.failed ? <Text type="supporting" color="secondary">{t("{n} échec(s)").replace("{n}", String(mig.failed))}</Text> : null}
            </VStack>
          ) : mig?.finished ? (
            <Banner status={mig.failed ? "warning" : "success"}
              title={(mig.failed
                ? t("Migration terminée : {d} fichier(s) déplacé(s), {f} en échec — relance-la pour réessayer.")
                : t("Migration terminée : {d} fichier(s) déplacé(s).")
              ).replace("{d}", String(mig.done ?? 0)).replace("{f}", String(mig.failed ?? 0))}
              description={mig.failed && mig.last_error ? mig.last_error : undefined} />
          ) : null}
          {!mig?.running && state.s3_ready && toMove > 0 && media === state.media_storage ? (
            <HStack gap={2} vAlign="center" wrap="wrap">
              <Text type="supporting">
                {t("{n} fichier(s) ne sont pas encore à l'emplacement choisi.").replace("{n}", String(toMove))}
              </Text>
              <Button label={state.media_storage === "s3" ? t("Migrer vers S3") : t("Rapatrier sur le volume")}
                variant="secondary" size="sm" onClick={() => void migrate(state.media_storage)} />
            </HStack>
          ) : null}
        </VStack>

        <VStack gap={2}>
          <Text weight="semibold" type="supporting">{t("Sauvegardes (chaque nuit à 3 h)")}</Text>
          <SegmentedControl label={t("Destination des sauvegardes")} value={dest}
            onChange={(v) => setDest(v as "local" | "s3" | "both")}
            isDisabled={!state.s3_ready && dest === "local"} disabledMessage={s3Hint}>
            <SegmentedControlItem value="local" label={t("Volume local")} />
            <SegmentedControlItem value="s3" label={t("S3")} />
            <SegmentedControlItem value="both" label={t("Les deux")} />
          </SegmentedControl>
          <Grid columns={{ minWidth: 220, max: 2 }} gap={4}>
            <NumberInput label={t("Rétention locale")} value={keepLocal} min={1} max={state.max_keep}
              description={t("Nombre de sauvegardes gardées sur la machine (une par nuit).")}
              isDisabled={dest === "s3"} disabledMessage={t("Les sauvegardes ne sont pas gardées en local.")}
              onChange={(v) => setKeepLocal(Math.round(v))} />
            <NumberInput label={t("Rétention S3")} value={keepS3} min={1} max={state.max_keep}
              description={t("Nombre de sauvegardes gardées dans le bucket.")}
              isDisabled={dest === "local"} disabledMessage={t("Les sauvegardes ne partent pas sur S3.")}
              onChange={(v) => setKeepS3(Math.round(v))} />
          </Grid>
          {last ? (
            <HStack gap={2} vAlign="center" wrap="wrap">
              <StatusDot variant={last.last_ok === false ? "error" : "success"}
                label={last.last_ok === false ? t("Échec") : t("OK")} />
              <Text type="supporting">
                {last.last_ok === false
                  ? t("Dernière tentative en échec ({e}). Dernière sauvegarde réussie : {d}.")
                    .replace("{e}", last.error ?? "?")
                    .replace("{d}", last.last_backup ? new Date(last.last_backup).toLocaleString(locale) : "—")
                  : t("Dernière sauvegarde : {d} — {f} ({s}).")
                    .replace("{d}", last.last_backup ? new Date(last.last_backup).toLocaleString(locale) : "—")
                    .replace("{f}", last.file ?? "?").replace("{s}", last.size ?? "?")}
              </Text>
            </HStack>
          ) : (
            <Text type="supporting" color="secondary">{t("Aucune sauvegarde enregistrée pour l'instant.")}</Text>
          )}
        </VStack>

        <HStack gap={2} hAlign="end">
          <Button label={t("Annuler")} variant="ghost" size="sm" isDisabled={!dirty || saving}
            onClick={() => void load()} />
          <Button label={saving ? t("Enregistrement…") : t("Enregistrer")} variant="primary" size="sm"
            isDisabled={!dirty || saving} onClick={() => void save()} />
        </HStack>
      </VStack>
    </Card>
  );
}
