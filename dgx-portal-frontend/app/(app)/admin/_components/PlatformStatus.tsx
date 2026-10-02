"use client";

import type { ReactNode } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { useT, useLocale } from "@/lib/i18n";

/** Response of GET /admin/platform (admin-only, implemented portal-side).
 * All keys are always present; an unknown value is null — the display
 * must NEVER turn a missing one into « tout va bien ». */
export type PlatformStatusData = {
  disk: { free_gb: number | null; total_gb: number | null; used_pct: number | null };
  backup: { latest: string | null; age_hours: number | null; fresh: boolean | null; count: number | null; readable: boolean };
  monitor: { readable: boolean; active_incidents: string[]; since: string | null };
  model: { name: string | null; status: string | null; uptime_s: number | null };
  portal: { db_mb: number | null; active_sessions: number | null; local_users: number | null; active_keys: number | null };
  maintenance: boolean;
  checked_at: number | null;
};

/** « 10 h 36 » from uptime seconds: the human format, not a raw counter
 * nobody converts in their head. */
function fmtUptime(s: number, numLocale: string): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")}`;
  if (m > 0) return `${m} min`;
  return `${new Intl.NumberFormat(numLocale, { maximumFractionDigits: 0 }).format(s)} s`;
}

/** Compact summary row: status dot on the left, label, value on the right.
 * `dotVariant === null` = the row carries no state (portal counters),
 * so no dot at all — never an unearned green. */
function StatusRow({ label, dotVariant, dotLabel, value }: {
  label: string;
  dotVariant: "success" | "warning" | "error" | "neutral" | null;
  dotLabel?: string;
  value: ReactNode;
}) {
  return (
    <HStack gap={2} vAlign="center" hAlign="between" wrap="wrap">
      <HStack gap={2} vAlign="center">
        {dotVariant && <StatusDot variant={dotVariant} label={dotLabel ?? label} />}
        <Text weight="semibold">{label}</Text>
      </HStack>
      <Text type="supporting" color="secondary" wordBreak="break-all">{value}</Text>
    </HStack>
  );
}

export function PlatformStatus({ status }: { status: PlatformStatusData | null }) {
  const t = useT();
  const numLocale = useLocale();
  const fmt = (n: number, digits = 1) => new Intl.NumberFormat(numLocale, { maximumFractionDigits: digits }).format(n);

  // Disk: alert when free space gets low OR the fill level exceeds
  // 90 % — the operator's two conditions, tested independently.
  const freeGb = status?.disk.free_gb ?? null;
  const usedPct = status?.disk.used_pct ?? null;
  const diskKnown = freeGb !== null || usedPct !== null;
  const diskWarn = (freeGb !== null && freeGb < 20) || (usedPct !== null && usedPct >= 90);
  const diskParts: string[] = [];
  if (usedPct !== null) diskParts.push(`${fmt(usedPct)} %`);
  if (freeGb !== null) {
    // The units live INSIDE the i18n key (« Go » → « GB » in English).
    diskParts.push(
      status?.disk.total_gb != null
        ? t("{v} Go sur {w} Go libres").replace("{v}", fmt(freeGb)).replace("{w}", fmt(status.disk.total_gb))
        : t("{v} Go libre").replace("{v}", fmt(freeGb)),
    );
  }

  // Backup: unreadable = the state is degraded (we say so); otherwise alert
  // as soon as the dump exceeds the monitor's window (26 h) or is not fresh.
  const backup = status?.backup ?? null;
  const backupUnreadable = backup !== null && backup.readable === false;
  const backupStale = backup !== null && backup.readable !== false &&
    (backup.fresh === false || backup.age_hours === null || backup.age_hours > 26);
  const backupParts: string[] = [];
  if (backup?.latest) backupParts.push(backup.latest);
  if (backup?.age_hours != null) backupParts.push(t("il y a {h} h").replace("{h}", fmt(backup.age_hours)));
  else if (backup !== null && !backupUnreadable) backupParts.push(t("âge inconnu"));
  if (backup?.count != null) backupParts.push(`${fmt(backup.count, 0)} ${t("archives")}`);

  // Monitor: active incidents = alert; unreadable = we never claim
  // « aucun incident » on data we have not read.
  const monitor = status?.monitor ?? null;
  const incidents = monitor?.active_incidents ?? [];
  const monitorUnreadable = monitor !== null && monitor.readable === false;
  const monitorValue = monitorUnreadable
    ? t("état non lisible")
    : incidents.length > 0
      ? `${fmt(incidents.length, 0)} ${t("incident(s) actif(s)")} — ${incidents.join(", ")}`
      : t("aucun incident");

  // Served model: same state vocabulary as the Backends card.
  const model = status?.model ?? null;
  const modelStatus = model?.status ?? null;
  const modelVariant = modelStatus === "running" ? "success" : modelStatus === "starting" ? "warning" : "neutral";
  const modelLabel = modelStatus === "running" ? t("En ligne") : modelStatus === "starting" ? t("Démarrage…") : modelStatus === "stopped" ? t("Arrêté") : t("état inconnu");
  const modelParts: string[] = [];
  if (model?.name) {
    modelParts.push(model.name);
    // Uptime only means something next to a known name.
    if (model.uptime_s != null && modelStatus !== null) modelParts.push(t("en service depuis {u}").replace("{u}", fmtUptime(model.uptime_s, numLocale)));
  } else if (modelStatus !== null) {
    modelParts.push(t("aucun modèle"));
  }

  // Portal: raw counters, no state semantics → no dot.
  const portal = status?.portal ?? null;
  const portalParts: string[] = [];
  if (portal?.db_mb != null) portalParts.push(t("base {v} Mo").replace("{v}", fmt(portal.db_mb)));
  if (portal?.active_sessions != null) portalParts.push(`${fmt(portal.active_sessions, 0)} ${t("sessions actives")}`);
  if (portal?.local_users != null) portalParts.push(`${fmt(portal.local_users, 0)} ${t("comptes locaux")}`);
  if (portal?.active_keys != null) portalParts.push(`${fmt(portal.active_keys, 0)} ${t("clés actives")}`);

  const checkedAt = status?.checked_at != null ? new Date(status.checked_at * 1000) : null;

  return (
    <Card>
      <VStack gap={2}>
        <HStack gap={2} vAlign="center" hAlign="between" wrap="wrap">
          <HStack gap={2} vAlign="center">
            <Text weight="semibold">{t("État de la plateforme")}</Text>
            {status?.maintenance && <Badge label={t("Maintenance")} variant="warning" />}
          </HStack>
          {checkedAt && !Number.isNaN(checkedAt.getTime()) && (
            <Text type="supporting" color="secondary">{t("Vérifié à {time}").replace("{time}", checkedAt.toLocaleTimeString(numLocale))}</Text>
          )}
        </HStack>

        {!status ? (
          // No data (loading or failed snapshot): we write it as-is
          // rather than draw a green summary with no source.
          <Text type="supporting" color="secondary">{t("Relevé indisponible — impossible de lire l'état de la plateforme.")}</Text>
        ) : (
          <VStack gap={2}>
            <StatusRow
              label={t("Disque")}
              dotVariant={diskKnown ? (diskWarn ? "warning" : "success") : "neutral"}
              dotLabel={diskKnown ? (diskWarn ? t("Espace disque faible") : t("Espace disque suffisant")) : t("état inconnu")}
              value={diskParts.length > 0 ? diskParts.join(" · ") : "—"}
            />
            <StatusRow
              label={t("Sauvegarde")}
              dotVariant={backupUnreadable ? "neutral" : backupStale ? "warning" : backup !== null ? "success" : "neutral"}
              dotLabel={backupUnreadable ? t("sauvegarde illisible") : backupStale ? t("Sauvegarde en retard") : backup !== null ? t("Sauvegarde récente") : t("état inconnu")}
              value={backupUnreadable ? t("sauvegarde illisible") : backupParts.length > 0 ? backupParts.join(" · ") : "—"}
            />
            <StatusRow
              label={t("Moniteur")}
              dotVariant={monitor === null ? "neutral" : monitorUnreadable || incidents.length > 0 ? "warning" : "success"}
              dotLabel={monitorUnreadable ? t("état non lisible") : incidents.length > 0 ? t("incident(s) actif(s)") : t("aucun incident")}
              value={monitor === null ? "—" : monitorValue}
            />
            <StatusRow
              label={t("Modèle servi")}
              dotVariant={model === null ? "neutral" : modelVariant}
              dotLabel={model === null ? t("état inconnu") : modelLabel}
              value={modelParts.length > 0 ? modelParts.join(" · ") : "—"}
            />
            <StatusRow
              label={t("Portail")}
              dotVariant={null}
              value={portalParts.length > 0 ? portalParts.join(" · ") : "—"}
            />
          </VStack>
        )}
      </VStack>
    </Card>
  );
}
