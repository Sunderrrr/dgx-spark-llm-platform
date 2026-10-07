"use client";

// Vue d'ensemble — what the operator looks at ten times a day: state only,
// nothing to act on. Anything with a button lives on the other tabs
// (ONE ACTION = ONE TAB).

import { VStack } from "@astryxdesign/core/Stack";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { useT } from "@/lib/i18n";
import { PlatformStatus, type PlatformStatusData } from "./PlatformStatus";
import { LiveActivity } from "./LiveActivity";
import type { AdminTabProps } from "./adminTypes";

export function OverviewTab({ platform, data, actionDisabled, setConfirmAction }: {
  platform: PlatformStatusData | null;
} & Pick<AdminTabProps, "data" | "actionDisabled" | "setConfirmAction">) {
  const t = useT();
  return (
    <VStack gap={6}>
      {/* The maintenance switch lives HERE, not in Système: it is a STATE of
          the platform, and it is the one thing an operator wants to reach from
          the overview when something is wrong. Everything else with a button
          stays on its own tab (ONE ACTION = ONE TAB). */}
      {data && (
        <Banner
          status={data.maintenance_mode ? "warning" : "info"}
          title={data.maintenance_mode ? t("Mode maintenance actif") : t("Mode maintenance")}
          description={t(
            "Bloque l'accès à l'API et au chat/OCR/vidéo pour les non-admins, sans arrêter les modèles. Les admins gardent l'accès.",
          )}
          endContent={
            <Button
              label={data.maintenance_mode ? t("Désactiver") : t("Activer")}
              variant={data.maintenance_mode ? "secondary" : "primary"}
              size="sm"
              isDisabled={actionDisabled}
              onClick={() => setConfirmAction({ kind: "maintenance" })}
            />
          }
        />
      )}
      {/* Dashboard widget (disk, backup, monitor, served model, counters):
          refreshed by the same 8 s poll as the rest of the page — the data
          flows down, no second timer. */}
      <PlatformStatus status={platform} />
      {/* Work waiting for the operator — visible from the overview so a
          pending approval is never discovered by accident, but the ACTION
          stays on Demandes (ONE ACTION = ONE TAB): this is a signpost. */}
      {data && (data.stats.pending > 0 || data.stats.budget_pending > 0) ? (
        <Banner
          status="warning"
          title={t("{n} demande(s) en attente de décision").replace(
            "{n}",
            String(data.stats.pending + data.stats.budget_pending),
          )}
          description={t("Des modèles ou des budgets attendent ton approbation.")}
          endContent={
            <Button
              label={t("Ouvrir les demandes")}
              variant="secondary"
              size="sm"
              onClick={() => {
                if (typeof window !== "undefined") window.location.href = "/admin?tab=requests";
              }}
            />
          }
        />
      ) : null}
      <LiveActivity />
    </VStack>
  );
}
