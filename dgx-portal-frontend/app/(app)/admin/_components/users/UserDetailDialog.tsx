"use client";

// « Détails du compte » dialog of the admin users section: identity, budget,
// keys, sessions, audit. Extracted VERBATIM from
// app/(app)/admin/_components/UsersSection.tsx (plain props, no behaviour
// change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { List, ListItem } from "@astryxdesign/core/List";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Banner } from "@astryxdesign/core/Banner";
import { KeyIcon } from "@heroicons/react/24/outline";
import { useT, useLocale } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { UserAvatar } from "@/lib/user-avatar";
import { SessionsList } from "../../../_components/SessionsList";
import { SOURCE_META, type AdminUserDetail, type LocalUser } from "./shared";

export type UserDetailDialogProps = {
  detailUser: LocalUser | null;
  setDetailUser: (v: LocalUser | null) => void;
  detail: AdminUserDetail | null;
  detailLoading: boolean;
  detailError: string | null;
  detailSources: string[];
  fmtBudget: (n: number) => string;
  sessRevoking: boolean;
  revokeDetailSessions: () => void;
};

export function UserDetailDialog({
  detailUser,
  setDetailUser,
  detail,
  detailLoading,
  detailError,
  detailSources,
  fmtBudget,
  sessRevoking,
  revokeDetailSessions,
}: UserDetailDialogProps) {
  const t = useT();
  const numLocale = useLocale();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* Detail drawer — identity, budget, keys, sessions and audit of the account */}
      <Dialog isOpen={detailUser != null} onOpenChange={(o) => { if (!o) setDetailUser(null); }} purpose="form" width={isNarrow ? "94vw" : 640}>
        <Layout
          header={<DialogHeader title={t("Détails du compte")} subtitle={detailUser?.username} hasDivider onOpenChange={(o) => { if (!o) setDetailUser(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              {detailError ? <Banner status="error" title={detailError} /> : null}
              {detailLoading && !detail ? (
                <HStack hAlign="center" padding={4}>
                  <HStack width={200}>
                    <ProgressBar label={t("Chargement…")} isIndeterminate isLabelHidden />
                  </HStack>
                </HStack>
              ) : detail ? (
                <VStack gap={4}>
                  {/* Identity, role, sources, last activity */}
                  <HStack gap={3} vAlign="center">
                    <UserAvatar
                      avatarId={detailUser?.avatar_id}
                      username={detailUser?.username || detail.username}
                      name={detail.fullname || detail.username || detailUser?.username}
                      size="md"
                    />
                    <VStack gap={0}>
                      <HStack gap={2} vAlign="center" wrap="wrap">
                        <Text weight="semibold">{detail.username ?? detailUser?.username}</Text>
                        {detail.role === "admin" ? <Badge label={t("Admin")} variant="warning" /> : null}
                        {detail.blocked?.blocked ? <Badge label={t("Bloqué")} variant="error" /> : null}
                        {detail.enabled === false ? <Badge label={t("Désactivé")} variant="error" /> : null}
                      </HStack>
                      {detail.fullname ? <Text type="supporting" color="secondary">{detail.fullname}</Text> : null}
                      <HStack gap={1} vAlign="center" wrap="wrap">
                        {detailSources.map((s) => {
                          const m = SOURCE_META[s] ?? { label: s, variant: "neutral" as const };
                          return <Badge key={s} label={t(m.label)} variant={m.variant} />;
                        })}
                        {detail.last_source ? (
                          <Text type="supporting" color="secondary">
                            {t("Dernière source")} : {detail.last_source.toUpperCase()}
                          </Text>
                        ) : null}
                      </HStack>
                      {detail.last_seen ? (
                        <Text type="supporting" color="secondary">
                          {t("Dernière activité")} : <Timestamp value={detail.last_seen} format="date_time" />
                        </Text>
                      ) : null}
                    </VStack>
                  </HStack>

                  {/* Active block: reason, author, date */}
                  {detail.blocked?.blocked ? (
                    <HStack gap={2} vAlign="center" wrap="wrap">
                      <Badge label={t("Bloqué")} variant="error" />
                      {detail.blocked.reason ? <Text type="supporting" color="secondary">{detail.blocked.reason}</Text> : null}
                      {detail.blocked.by ? <Text type="supporting" color="secondary">{t("par")} {detail.blocked.by}</Text> : null}
                      {detail.blocked.at ? <Timestamp value={detail.blocked.at} format="date_time" /> : null}
                    </HStack>
                  ) : null}

                  {/* Budget: override → effective, and LiteLLM spend */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Budget")}</Text>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Surcharge utilisateur")}</Text>
                      <Text hasTabularNumbers>{detail.max_budget != null ? fmtBudget(detail.max_budget) : "—"}</Text>
                    </HStack>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Quota effectif")}</Text>
                      <Text hasTabularNumbers>
                        {detail.effective_budget != null ? `${fmtBudget(detail.effective_budget)} ${t(detail.effective_budget > 1 ? "tokens" : "token")}` : "—"}
                      </Text>
                    </HStack>
                    <HStack hAlign="between">
                      <Text type="supporting" color="secondary">{t("Dépensé (LiteLLM)")}</Text>
                      <Text hasTabularNumbers>
                        {detail.litellm?.exists ? `${fmtBudget(detail.litellm.spend ?? 0)} ${t((detail.litellm.spend ?? 0) > 1 ? "tokens" : "token")}` : t("Aucun profil LiteLLM")}
                      </Text>
                    </HStack>
                    {detail.litellm?.budget_duration ? (
                      <HStack hAlign="between">
                        <Text type="supporting" color="secondary">{t("Fenêtre de budget")}</Text>
                        <Text>{detail.litellm.budget_duration}</Text>
                      </HStack>
                    ) : null}
                    {detail.group ? (
                      <HStack hAlign="between">
                        <Text type="supporting" color="secondary">{t("Groupe")}</Text>
                        <Text>{detail.group}</Text>
                      </HStack>
                    ) : null}
                  </VStack>

                  {/* Keys — alias, creation, spend. Never any key value:
                      the payload cannot contain any. */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Clés")}</Text>
                    {(detail.keys ?? []).length === 0 ? (
                      <Text type="supporting" color="secondary">{t("Aucune clé pour l'instant.")}</Text>
                    ) : (
                      <List hasDividers>
                        {(detail.keys ?? []).map((k, i) => (
                          <ListItem
                            key={`${k.alias ?? ""}-${i}`}
                            startContent={<Icon icon={KeyIcon} size="sm" color="secondary" />}
                            label={k.alias || t("Sans nom")}
                            description={k.created_at ? <Timestamp value={k.created_at} format="date_time" /> : undefined}
                            endContent={
                              <Text type="supporting" color="secondary" hasTabularNumbers>
                                {`${Math.round(k.spend ?? 0).toLocaleString(numLocale)} ${t(Math.round(k.spend ?? 0) > 1 ? "tokens" : "token")}`}
                              </Text>
                            }
                          />
                        ))}
                      </List>
                    )}
                  </VStack>

                  {/* Memory & conversations */}
                  <HStack gap={4} vAlign="center">
                    <HStack gap={1} vAlign="center">
                      <Text type="supporting" color="secondary">{t("Mémoire")} :</Text>
                      <Text hasTabularNumbers>{detail.memory_facts ?? 0}</Text>
                    </HStack>
                    <HStack gap={1} vAlign="center">
                      <Text type="supporting" color="secondary">{t("Conversations")} :</Text>
                      <Text hasTabularNumbers>{detail.conversations ?? 0}</Text>
                    </HStack>
                  </HStack>

                  {/* Sessions — same layout as the self-service list */}
                  <VStack gap={2}>
                    <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
                      <Text weight="semibold">{t("Sessions")}</Text>
                      <Button
                        label={t("Révoquer toutes ses sessions")}
                        variant="secondary"
                        size="sm"
                        isLoading={sessRevoking}
                        isDisabled={(detail.sessions ?? []).length === 0}
                        onClick={revokeDetailSessions}
                      />
                    </HStack>
                    <SessionsList sessions={detail.sessions ?? []} />
                  </VStack>

                  {/* Recent audit */}
                  <VStack gap={2}>
                    <Text weight="semibold">{t("Dernières actions de ce compte")}</Text>
                    {(detail.audit ?? []).length === 0 ? (
                      <Text type="supporting" color="secondary">{t("Aucune entrée")}</Text>
                    ) : (
                      <List hasDividers>
                        {(detail.audit ?? []).map((a, i) => (
                          <ListItem
                            key={`${a.action ?? ""}-${i}`}
                            label={a.action || "—"}
                            description={
                              <VStack gap={0}>
                                {a.detail ? <Text type="supporting" color="secondary">{a.detail}</Text> : null}
                                <HStack gap={1} vAlign="center">
                                  {a.by ? <Text type="supporting" color="secondary">{a.by}</Text> : null}
                                  {a.at ? <Timestamp value={a.at} format="date_time" /> : null}
                                </HStack>
                              </VStack>
                            }
                          />
                        ))}
                      </List>
                    )}
                  </VStack>
                </VStack>
              ) : null}
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Fermer")} variant="ghost" onClick={() => setDetailUser(null)} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
