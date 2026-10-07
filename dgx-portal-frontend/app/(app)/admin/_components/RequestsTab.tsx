"use client";

// Demandes — the queue of things waiting for the operator's verdict: model
// requests and token requests, with their approve/refuse actions. One action =
// one tab: « I want to approve » lands here, no scrolling.

import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Table } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { CheckIcon, XMarkIcon } from "@heroicons/react/24/outline";
import { useT, useLocale } from "@/lib/i18n";
import {
  REQ_STATUS_LABEL,
  REQ_STATUS_VARIANT,
  fmtCompact,
  type AdminTabProps,
  type BudgetRequest,
  type ModelRequest,
} from "./adminTypes";

const BUDGET_PRESETS = [10000000, 50000000, 100000000];

export function RequestsTab({ data, act, actionDisabled }: AdminTabProps) {
  const t = useT();
  const numLocale = useLocale();

  const budgetColumns: TableColumn<BudgetRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: t("Utilisateur"), renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "key_alias", header: t("Clé") },
    { key: "current_budget", header: t("Budget actuel"), renderCell: (r) => (r.current_budget ? Math.round(r.current_budget).toLocaleString(numLocale) : "—") },
    { key: "reason", header: t("Raison"), renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    {
      key: "status",
      header: t("Statut"),
      renderCell: (r) =>
        r.status === "pending" ? (
          <Badge label={t("En attente")} variant="warning" />
        ) : r.status === "approved" ? (
          <Badge label={`+${Math.round(r.granted_amount || 0).toLocaleString(numLocale)} ✓`} variant="success" />
        ) : (
          <Badge label={t("Refusé")} variant="error" />
        ),
    },
    {
      key: "id" as keyof BudgetRequest,
      header: t("Action"),
      renderCell: (r) =>
        r.status === "pending" ? (
          <HStack gap={1}>
            <BudgetApproveForm fullname={r.fullname} currentBudget={r.current_budget} isDisabled={actionDisabled} onApprove={(amount, days) => act(`/admin/budget/approve/${r.id}`, { amount, grant_days: days })} />
            <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/budget/reject/${r.id}`)} />
          </HStack>
        ) : null,
    },
  ];

  const requestColumns: TableColumn<ModelRequest & Record<string, unknown>>[] = [
    { key: "fullname", header: t("Utilisateur"), renderCell: (r) => `${r.fullname} (${r.username})` },
    { key: "model_id", header: t("Modèle") },
    { key: "reason", header: t("Raison"), renderCell: (r) => r.reason || "—" },
    { key: "created_at", header: t("Date"), renderCell: (r) => r.created_at.slice(0, 16).replace("T", " ") },
    { key: "status", header: t("Statut"), renderCell: (r) => <Badge label={t(REQ_STATUS_LABEL[r.status] || r.status)} variant={REQ_STATUS_VARIANT[r.status] || "neutral"} /> },
    {
      key: "id" as keyof ModelRequest,
      header: t("Action"),
      renderCell: (r) => (
        <HStack gap={1}>
          {r.status !== "done" && <Button label={t("Lancé")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={CheckIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/update/${r.id}`, { status: "done" })} />}
          {r.status !== "rejected" && <Button label={t("Refuser")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/update/${r.id}`, { status: "rejected" })} />}
        </HStack>
      ),
    },
  ];

  return (
    <VStack gap={6}>
      {data && (
        <Grid columns={3} gap={3}>
          <Card>
            <VStack gap={0} align="center">
              <Text size="2xl" weight="bold">{data.stats.pending}</Text>
              <Text type="supporting" color="secondary">{t("Demandes en attente")}</Text>
            </VStack>
          </Card>
          <Card>
            <VStack gap={0} align="center">
              <Text size="2xl" weight="bold" color="accent">{data.stats.done}</Text>
              <Text type="supporting" color="secondary">{t("Lancées")}</Text>
            </VStack>
          </Card>
          <Card>
            <VStack gap={0} align="center">
              <Text size="2xl" weight="bold">{data.stats.rejected}</Text>
              <Text type="supporting" color="secondary">{t("Refusées")}</Text>
            </VStack>
          </Card>
        </Grid>
      )}

      <VStack gap={2}>
        <HStack gap={2} vAlign="center">
          <Text weight="semibold">{t("Demandes de tokens")}</Text>
          {data && data.stats.budget_pending > 0 && <Badge label={`${data.stats.budget_pending} ${t("en attente")}`} variant="warning" />}
        </HStack>
        <Card padding={0}>
          <Table<BudgetRequest & Record<string, unknown>> data={data?.budget_reqs ?? []} columns={budgetColumns} idKey="id" density="balanced" dividers="rows" />
        </Card>
      </VStack>

      <VStack gap={2}>
        <Text weight="semibold">{t("Demandes de modèles")}</Text>
        <Card padding={0}>
          <Table<ModelRequest & Record<string, unknown>> data={data?.requests ?? []} columns={requestColumns} idKey="id" density="balanced" dividers="rows" />
        </Card>
      </VStack>
    </VStack>
  );
}

function BudgetApproveForm({ onApprove, fullname, currentBudget, isDisabled }: {
  onApprove: (amount: string, days: string) => void;
  fullname: string;
  currentBudget: number | null;
  /** During a whole-page action (or without a CSRF token): neither the opening of
      the dialog nor its confirmation must be sent. */
  isDisabled?: boolean;
}) {
  /** ONE button per request: « Approuver » opens a dialog with one-click amounts
     (+10M/+50M/+100M), a duration in segments (Permanente/1j/3j/7j/30j)
     and a preview of the result BEFORE confirming. No field left to guess. */
  const t = useT();
  const numLocale = useLocale();
  const [isOpen, setIsOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [days, setDays] = useState("permanent");
  const fmt = (n: number) => Math.round(n).toLocaleString(numLocale);
  const base = currentBudget || 0;
  const total = base + (parseFloat(amount) || 0);
  // eslint-disable-next-line react-hooks/purity -- « retour à la base le … » preview: the date is by nature relative to now, recomputed at each opening of the dialog
  const expire = days === "permanent" ? null : new Date(Date.now() + parseInt(days, 10) * 86400000);
  return (
    <>
      <Button label={t("Approuver")} variant="ghost" size="sm" isDisabled={isDisabled} onClick={() => setIsOpen(true)} />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Accorder des tokens")} subtitle={`${fullname} — ${t("Actuel :")} ${fmt(base)}`} />
        <VStack gap={3}>
          <VStack gap={1}>
            <Text type="supporting" color="secondary">{t("Montant à ajouter")}</Text>
            <HStack gap={1}>
              {BUDGET_PRESETS.map((v) => (
                <Button key={v} label={`+${fmtCompact(v, numLocale)}`} variant="secondary" size="sm" onClick={() => setAmount(String(v))} />
              ))}
              <TextInput label={t("Montant (tokens)")} isLabelHidden value={amount} onChange={setAmount} placeholder={t("Autre montant...")} size="sm" />
            </HStack>
          </VStack>
          <VStack gap={1}>
            <Text type="supporting" color="secondary">{t("Durée du supplément")}</Text>
            <SegmentedControl value={days} onChange={setDays} label={t("Durée du supplément")} size="sm">
              <SegmentedControlItem value="permanent" label={t("Permanente")} />
              <SegmentedControlItem value="1" label={t("1 j")} />
              <SegmentedControlItem value="3" label={t("3 j")} />
              <SegmentedControlItem value="7" label={t("7 j")} />
              <SegmentedControlItem value="30" label={t("30 j")} />
            </SegmentedControl>
          </VStack>
          {amount && (
            <Text type="supporting" color="secondary">
              {days === "permanent"
                ? `${t("Nouveau total :")} ${fmt(total)}`
                : t("Nouveau total : {total} — retour à {base} le {date} UTC.")
                    .replace("{total}", fmt(total))
                    .replace("{base}", fmt(base))
                    .replace("{date}", expire ? expire.toISOString().slice(0, 16).replace("T", " ") : "")}
            </Text>
          )}
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Confirmer")}
              variant="primary"
              size="sm"
              isDisabled={!amount || isDisabled}
              onClick={() => {
                onApprove(amount, days === "permanent" ? "" : days);
                setIsOpen(false);
                setAmount("");
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}
