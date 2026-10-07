"use client";

// « Fenêtre de contexte » dialog: what the model sees for this turn (usage,
// input/output split, breakdown, files). Extracted VERBATIM from
// app/(app)/playground/page.tsx (plain props, no behaviour change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { useT, useLocale } from "@/lib/i18n";
import type { Attachment } from "@/lib/types";
import { fmtK } from "./ContextMeter";

export type ContextDialogProps = {
  ctxOpen: boolean;
  setCtxOpen: (v: boolean) => void;
  used: number;
  max: number;
  inTokens: number;
  outTokens: number;
  hasIo: boolean;
  sysPct: number;
  msgPct: number;
  ctxLevel: "accent" | "warning" | "error";
  model: string;
  convTokens: number;
  systemPrompt: string;
  attachments: Attachment[];
};

export function ContextDialog({
  ctxOpen,
  setCtxOpen,
  used,
  max,
  inTokens,
  outTokens,
  hasIo,
  sysPct,
  msgPct,
  ctxLevel,
  model,
  convTokens,
  systemPrompt,
  attachments,
}: ContextDialogProps) {
  const t = useT();
  const numLocale = useLocale();
  return (
          <Dialog isOpen={ctxOpen} onOpenChange={(o) => { if (!o) setCtxOpen(false); }} width={560}>
            <DialogHeader
              title={t("Fenêtre de contexte")}
              subtitle={t("Ce que le modèle voit pour ce tour")}
              hasDivider
              onOpenChange={(o) => { if (!o) setCtxOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              {/* Total + barre de progression */}
              <VStack gap={1}>
                <HStack hAlign="between" vAlign="center">
                  <Text weight="semibold">{t("Utilisation du contexte")}</Text>
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {fmtK(used)} / {fmtK(max)} {t("tokens")}
                  </Text>
                </HStack>
                <ProgressBar label={t("Utilisation du contexte")} isLabelHidden value={used} max={max} variant={ctxLevel} />
              </VStack>
              {/* Entrée / sortie : le contexte n'est pas un bloc opaque — on sépare
                  ce qui le remplit (prompt) de ce qui est produit (réponse). */}
              <VStack gap={1}>
                <Text type="label">{t("Entrée / sortie")}</Text>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="accent" label={t("Entrée (prompt)")} />
                  <Text type="supporting">{t("Entrée (prompt)")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{inTokens.toLocaleString(numLocale)} {t("tokens")}</Text>
                </HStack>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="neutral" label={t("Sortie (généré)")} />
                  <Text type="supporting">{t("Sortie (généré)")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {hasIo ? `${outTokens.toLocaleString(numLocale)} ${t("tokens")}` : "—"}
                  </Text>
                </HStack>
              </VStack>
              {/* Répartition : prompt système vs messages/fichiers */}
              <VStack gap={1}>
                <Text type="label">{t("Répartition")}</Text>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="accent" label={t("System prompt")} />
                  <Text type="supporting">{t("System prompt")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{sysPct} %</Text>
                </HStack>
                <HStack gap={2} vAlign="center">
                  <StatusDot variant="neutral" label={t("Messages")} />
                  <Text type="supporting">{t("Messages")}</Text>
                  <StackItem size="fill" />
                  <Text type="supporting" color="secondary" hasTabularNumbers>{msgPct} %</Text>
                </HStack>
              </VStack>
              <HStack gap={2} vAlign="center" wrap="wrap">
                {model && <Badge label={model} variant="info" />}
                <Badge label={`${Math.round((used / max) * 100)} % ${t("contexte")}`} variant="info" />
                {convTokens > 0 && (
                  <Badge label={`${convTokens.toLocaleString(numLocale)} ${t("tokens")}`} variant="info" />
                )}
              </HStack>
              {systemPrompt ? (
                <VStack gap={1}>
                  <Text type="label">{t("System prompt")}</Text>
                  <Text type="supporting" color="secondary">{systemPrompt}</Text>
                </VStack>
              ) : (
                <Text type="supporting" color="secondary">{t("Aucun system prompt.")}</Text>
              )}
              <VStack gap={1}>
                <Text type="label">{t("Fichiers joints")}</Text>
                {attachments.length === 0 ? (
                  <Text type="supporting" color="secondary">{t("Aucun fichier.")}</Text>
                ) : (
                  attachments.map((f, i) => (
                    <HStack key={i} gap={2} vAlign="center">
                      <Text type="supporting" color="secondary">{f.name}</Text>
                      <Text type="supporting" color="secondary">
                        {f.image ? t("image") : `${Math.ceil(f.content.length / 1024)} Ko`}
                      </Text>
                    </HStack>
                  ))
                )}
              </VStack>
              <Text type="supporting" color="secondary">
                {t("La fenêtre de contexte = entrée (system prompt + messages + fichiers) + sortie (réponse générée). Le % indique la part utilisée.")}
              </Text>
            </VStack>
          </Dialog>
  );
}
