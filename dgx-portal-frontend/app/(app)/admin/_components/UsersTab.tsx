"use client";

// Utilisateurs — the accounts and their envelopes: who exists, what they
// consume, and the quotas. One action = one tab: « I want to manage an
// account or a quota » lands here; the REQUESTS for quota land on Demandes.

import { useEffect, useRef, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { CheckIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { postFormVerifie } from "@/lib/api";
import { useT, useLocale, tServeur } from "@/lib/i18n";
import { UserLookup } from "./UserLookup";
import { UsersSection } from "./UsersSection";
import { fmtCompact, type AdminTabProps, type SpendRow } from "./adminTypes";

export function UsersTab({ data, act, actionDisabled }: AdminTabProps) {
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  // Default quotas form. Pre-filled on the FIRST load only: re-syncing it on
  // every 8 s poll overwrote the admin's input under their fingers, and it was
  // the stale value that was sent on the click on « Appliquer ».
  const [settings, setSettings] = useState({ budget: "", duration: "" });
  const filled = useRef(false);
  useEffect(() => {
    if (!data || filled.current) return;
    filled.current = true;
    setSettings({ budget: String(data.default_key_budget), duration: data.default_key_duration });
  }, [data]);

  return (
    <VStack gap={6}>
      <UserLookup
        spend={data?.spend_data ?? []}
        ocr={data?.ocr_usage ?? []}
        video={data?.video_usage ?? []}
        voice={data?.voice_usage ?? []}
        requests={data?.requests ?? []}
      />

      {/* Local accounts, groups, roles, blocking: full management of who can
          log in. Same component as the /users page (which stays where it is). */}
      <UsersSection csrf={csrf} />

      <VStack gap={2}>
        <Text weight="semibold">{t("Limite de tokens par défaut (nouvelles clés)")}</Text>
        <Card>
          <HStack gap={2} vAlign="end" wrap="wrap">
            <TextInput label={t("Tokens générés")} value={settings.budget} onChange={(v) => setSettings((s) => ({ ...s, budget: v }))} size="sm" />
            <TextInput label={t("Durée (ex: 1d, 7d, 12h)")} value={settings.duration} onChange={(v) => setSettings((s) => ({ ...s, duration: v }))} size="sm" />
            <Button
              label={t("Appliquer")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={CheckIcon} size="sm" />}
              isDisabled={actionDisabled}
              onClick={() => act("/admin/settings", { default_key_budget: settings.budget, default_key_duration: settings.duration })}
            />
          </HStack>
        </Card>
      </VStack>

      <BudgetSetForm rows={data?.spend_data ?? []} actionDisabled={actionDisabled} />

      {(data?.budget_grants?.length ?? 0) > 0 && (
        <VStack gap={1}>
          {data!.budget_grants.map((g) => (
            <Text key={g.username} type="supporting" color="secondary">
              {t("Boost temporaire — {user} : {total} tokens jusqu'au {date} UTC (retour à {base}).")
                .replace("{user}", g.username)
                .replace("{total}", Math.round(g.current_budget).toLocaleString(numLocale))
                .replace("{date}", g.expires_at.slice(0, 16).replace("T", " "))
                .replace("{base}", Math.round(g.base_budget).toLocaleString(numLocale))}
            </Text>
          ))}
        </VStack>
      )}
    </VStack>
  );
}

function BudgetSetForm({ rows, actionDisabled }: { rows: SpendRow[]; actionDisabled?: boolean }) {
  /** Reset an account's cap: EXACT amount (not an addition) — just as useful
     to LOWER a quota (200M → 50M) as to raise it. The user is
     CHOSEN from a list (with their current cap displayed): no name to
     type blindly. */
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [user, setUser] = useState("");
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);
  const sel = rows.find((r) => r.username === user);
  return (
    <Card>
      <VStack gap={2}>
        <Text weight="semibold">{t("Redéfinir le plafond d'un compte")}</Text>
        <Text type="supporting" color="secondary">
          {t("Montant exact (pas un ajout) — sert aussi à baisser un quota. La fenêtre de reset reste celle du portail.")}
        </Text>
        <Selector
          label={t("Utilisateur")}
          hasSearch
          searchPlaceholder={t("Rechercher...")}
          placeholder={t("Choisir un compte")}
          options={rows.map((r) => ({
            value: r.username,
            label: `${r.username} · ${r.unlimited ? t("illimité") : fmtCompact(r.max_budget || 0, numLocale)}`,
          }))}
          value={user}
          onChange={setUser}
        />
        {sel && (
          <Text type="supporting" color="secondary">
            {t("Plafond actuel :")} {sel.unlimited ? t("Illimitée (admin)") : fmtCompact(sel.max_budget || 0, numLocale)}
          </Text>
        )}
        <HStack gap={1}>
          {[50000000, 100000000, 200000000].map((v) => (
            <Button key={v} label={fmtCompact(v, numLocale)} variant="secondary" size="sm" onClick={() => setBudget(String(v))} />
          ))}
        </HStack>
        <HStack gap={2} vAlign="end">
          <TextInput label={t("Nouveau plafond (tokens)")} value={budget} onChange={setBudget} placeholder="50000000" size="sm" />
          <Button
            label={t("Redéfinir")}
            variant="primary"
            size="sm"
            isDisabled={!user || !budget || busy || actionDisabled}
            onClick={async () => {
              setBusy(true);
              try {
                // The verdict is READ: the route explicitly refuses (400) an
                // absurd cap (0, negative, > 1e12 — the hole of a 6.7e12
                // typed amount). With `postForm` alone, this refusal displayed
                // « Budget redéfini. ».
                const res = await postFormVerifie(
                  `/admin/users/${encodeURIComponent(user.trim())}/budget/set`,
                  csrf, { budget: budget.trim() },
                );
                if (!res.ok) {
                  showToast({ body: res.error ? tServeur(res.error, t) : t("L'action a échoué."), type: "error" });
                  return;
                }
                showToast({ body: t("Budget redéfini.") });
                setBudget("");
              } finally {
                setBusy(false);
              }
            }}
          />
        </HStack>
      </VStack>
    </Card>
  );
}
