"use client";

// Système — the machine's own configuration: logs, maintenance mode,
// announcement (broadcast to Discord), instance identity, notification email,
// and the audit journal. One action = one tab.

import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { MegaphoneIcon, ShieldExclamationIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { BrandingConfig } from "./BrandingConfig";
import { EmailConfig } from "./EmailConfig";
import { LogsPanel, type LogKind } from "./LogsPanel";
import type { AdminTabProps, AuditRow } from "./adminTypes";

export function SystemTab({ data, act, actionDisabled,  audit, logKind, onLogKindChange, logs, sidecarLogs }: AdminTabProps & {
  audit: AuditRow[];
  logKind: LogKind;
  onLogKindChange: (kind: LogKind) => void;
  logs: string[];
  sidecarLogs: string[];
}) {
  const t = useT();
  const [announce, setAnnounce] = useState({ title: "", body: "" });

  const auditColumns: TableColumn<AuditRow>[] = [
    { key: "username", header: t("Acteur") },
    { key: "action", header: t("Action"), renderCell: (r) => <Badge label={r.action} variant="neutral" /> },
    { key: "detail", header: t("Détail") },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
  ];

  return (
    <VStack gap={6}>
      <LogsPanel
        model={data?.v_status.model ?? null}
        logKind={logKind}
        onLogKindChange={onLogKindChange}
        logs={logs}
        sidecarLogs={sidecarLogs}
      />

      {/* Announcements are recorded then broadcast (Discord DM + webhook):
          this is the only Discord-facing control of the admin. */}
      <Card>
        <VStack gap={2}>
          <Text type="supporting" color="secondary">{t("Publier une annonce")}</Text>
          <HStack gap={2} wrap="wrap">
            <TextInput label={t("Titre")} isLabelHidden value={announce.title} onChange={(v) => setAnnounce((s) => ({ ...s, title: v }))} placeholder={t("Titre")} size="sm" />
            <TextInput label={t("Détails")} isLabelHidden value={announce.body} onChange={(v) => setAnnounce((s) => ({ ...s, body: v }))} placeholder={t("Détails (optionnel)")} size="sm" />
            <Button
              label={t("Publier")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={MegaphoneIcon} size="sm" />}
              isDisabled={actionDisabled}
              onClick={async () => {
                await act("/admin/announce", announce);
                setAnnounce({ title: "", body: "" });
              }}
            />
          </HStack>
        </VStack>
      </Card>

      {/* The instance identity goes FIRST: it is what the operator
          changes once, at deploy time. */}
      <BrandingConfig initialName={undefined} />
      <EmailConfig />

      <VStack gap={2}>
        <Text weight="semibold">{t("Journal d'audit")}</Text>
        <Card padding={0}>
          <Table<AuditRow> data={audit} columns={auditColumns} idKey="id" density="balanced" dividers="rows" emptyState={<EmptyState icon={<Icon icon={ShieldExclamationIcon} size="lg" color="secondary" />} title={t("Aucune entrée")} description={t("Les actions sensibles apparaîtront ici.")} />} />
        </Card>
      </VStack>
    </VStack>
  );
}
