"use client";

import { useEffect, useState } from "react";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Table, proportional, pixel } from "@astryxdesign/core/Table";
import type { TableColumn } from "@astryxdesign/core/Table";
import { TabList, Tab } from "@astryxdesign/core/TabList";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { useToast } from "@astryxdesign/core/Toast";
import {
  PlusIcon, TrashIcon, EyeIcon, EyeSlashIcon, KeyIcon,
  ClipboardDocumentIcon, PencilIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { authFetch, getJSON } from "@/lib/api";
import { copierTexte } from "@/lib/copier";
import {
  INTEGRATION_TOOLS,
  buildSnippet,
  snippetLanguage,
  type IntegrationTool,
  type ModelLimit,
} from "@/lib/integrationSnippets";
import { useT, useLocale } from "@/lib/i18n";

type DiscordStatus = { linkable: boolean; dm_enabled: boolean; linked: boolean; discord_name: string };
type ApiKey = {
  key_alias: string;
  key: string;
  created_at: string;
  // `last_active` comes from LiteLLM (last call with this key): it is what
  // makes it possible to spot a forgotten key without revoking it blindly.
  last_active: string | null;
  spend: number;
};
type Account = { spend: number; max_budget: number; budget_reset_at: string | null; unlimited: boolean; has_pending: boolean };
type KeysData = {
  user_keys: ApiKey[];
  budget_tokens: string;
  budget_duration: string;
  account: Account;
  model_limits: Record<string, ModelLimit>;
  running_models: string[];
  auto_model: string;
  public_api_url: string;
};

export function KeysContent() {
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const [data, setData] = useState<KeysData | null>(null);
  const [keyName, setKeyName] = useState("");
  const [budgetReason, setBudgetReason] = useState("");
  const [showBudgetForm, setShowBudgetForm] = useState(false);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [revealKeyInSnippet, setRevealKeyInSnippet] = useState(false);
  const [selectedKey, setSelectedKey] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [tool, setTool] = useState<IntegrationTool>("opencode");
  const [discord, setDiscord] = useState<DiscordStatus | null>(null);
  const [erreurChargement, setErreurChargement] = useState("");
  const [enCours, setEnCours] = useState(false);

  function refresh() {
    getJSON<KeysData>("/api/keys")
      .then((d) => {
        setErreurChargement("");
        setData(d);
        if (!selectedKey && d.user_keys.length) setSelectedKey(d.user_keys[0].key);
        // Default: the virtual "auto-model" (follows the currently-running model).
        if (!selectedModel) setSelectedModel(d.auto_model || d.running_models[0] || "");
      })
      // Before: no `catch`, so a failed request left the tab
      // almost empty (just the title) without the slightest explanation.
      .catch((e: Error) => setErreurChargement(e.message));
    getJSON<DiscordStatus>("/api/discord/status").then(setDiscord).catch(() => {});
  }

  /** Runs a form action and only says « c'est fait » when it is true.
   *
   * The four actions of this tab went through `postForm`, which returns
   * NEITHER the status NOR the response body: every failure (LiteLLM
   * unreachable, key not found, request already pending — refused with 409)
   * showed up as a success. Revocation was the worst case: the key stayed
   * valid while the user believed it dead.
   */
  async function actionKeys(
    corps: Record<string, string>,
    succes: string,
    apres?: (r: { key_alias?: string; key?: string }) => void,
  ): Promise<boolean> {
    if (!csrf) {
      // Without a message, the button became a silent no-op when both
      // `CsrfProvider` attempts had failed.
      showToast({ body: t("Session incomplète — recharge la page."), type: "error" });
      return false;
    }
    setEnCours(true);
    try {
      const res = await authFetch("/keys", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
        body: new URLSearchParams(corps).toString(),
      });
      const r = (await res.json().catch(() => ({}))) as {
        ok?: boolean; error?: string; code?: string; key_alias?: string; key?: string;
      };
      if (!res.ok || r.ok === false) {
        // A stable refusal carries a `code` (e.g. `deja_en_attente`): the UI
        // translates it, the server sentence is only a fallback.
        const msg = r.code ? t(r.code) : r.error;
        showToast({ body: msg ? t(msg) : t("L'action a échoué."), type: "error" });
        return false;
      }
      apres?.(r);
      showToast({ body: succes, type: "info" });
      refresh();
      return true;
    } catch {
      showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
      return false;
    } finally {
      setEnCours(false);
    }
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps -- should only run on mount
  useEffect(refresh, []);

  async function createKey() {
    await actionKeys({ action: "create", key_name: keyName }, t("Clé créée !"), (r) => {
      setKeyName("");
      // We show the new key right away: it is the only moment when it can be
      // seen without clicking « Afficher », and it is what one wants to copy.
      if (r.key) setRevealed((prev) => new Set(prev).add(r.key as string));
    });
  }

  async function revokeKey(key: string, alias: string) {
    // Revocation = permanent loss: we ask for confirmation, with the name.
    if (!window.confirm(t("Révoquer la clé « {alias} » ? Les programmes qui l'utilisent cesseront de fonctionner.").replace("{alias}", alias))) return;
    await actionKeys({ action: "revoke", key }, t("Clé révoquée."));
  }

  /** Copies the key WITHOUT revealing it.
   *
   * Previously one had to display the key, select it with the mouse then
   * copy it — three gestures, and the key stayed on the screen (in front of
   * the desk neighbour, in a screenshot, in a window share). The
   * « Afficher » button is for READING a key, not copying it.
   */
  function copyKey(key: string, alias: string) {
    void copierTexte(key, () =>
      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }),
    ).then((ok) => {
      if (ok) {
        showToast({
          body: t("Clé « {alias} » copiée.").replace("{alias}", alias),
          type: "info",
        });
      }
    });
  }

  /** Renames a key: the alias is what makes it possible to find one's way with
   *  several of them (one per machine) — it had to be chosen once and for all. */
  async function renameKey(key: string, alias: string) {
    const nouveau = window.prompt(
      t("Nouveau nom pour la clé « {alias} » :").replace("{alias}", alias), alias,
    );
    if (nouveau === null) return;
    const propre = nouveau.trim();
    if (!propre || propre === alias) return;
    await actionKeys({ action: "rename", key, key_name: propre }, t("Clé renommée."));
  }

  async function requestBudget() {
    const ok = await actionKeys(
      { action: "request_budget", reason: budgetReason },
      t("Demande de tokens envoyée !"),
    );
    if (ok) {
      setBudgetReason("");
      setShowBudgetForm(false);
    }
  }

  async function unlinkDiscord() {
    if (!csrf) return;
    try {
      const res = await authFetch("/discord/unlink", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
        body: "",
      });
      showToast({ body: res.ok ? t("Compte Discord délié.") : t("Le déliage a échoué."), type: res.ok ? "info" : "error" });
    } catch {
      showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
    }
    refresh();
  }

  // The widths are MANDATORY: without `width`, Astryx splits the width into
  // EQUAL shares (1/n) with no minimum. In the 706 px of the settings panel,
  // six columns made 118 px each and every row 177 px tall — the key wrapped
  // onto four lines and the buttons stacked up. So we set the share AND the
  // floor of each column, and the creation date moves under the alias to
  // give the key back the room it claims.
  const columns: TableColumn<ApiKey & Record<string, unknown>>[] = [
    {
      key: "key_alias",
      header: t("Alias"),
      width: proportional(3, { minWidth: 120 }),
      renderCell: (row) => (
        <VStack gap={0}>
          {/* 40 characters available: without truncation, a long alias made
              three lines and distorted the row. The tooltip shows it in full. */}
          <Text weight="semibold" maxLines={1}>{row.key_alias || "—"}</Text>
          {row.created_at && (
            <Text type="supporting" color="secondary">
              {`${t("Créée le")} ${new Date(row.created_at).toLocaleDateString(numLocale)}`}
            </Text>
          )}
        </VStack>
      ),
    },
    {
      key: "key",
      header: t("Clé"),
      width: proportional(5, { minWidth: 180 }),
      renderCell: (row) => (
        <HStack gap={2} vAlign="center" width="100%">
          {/* `StackItem size="fill"` brings flex:1 + min-width:0. Without this reset,
              a flex item refuses to shrink below its content: the key pushed
              the two buttons out of the cell. Hidden it fits on one line
              (truncated with a tooltip); REVEALED it unfolds on two lines
              instead of being cut — otherwise « Afficher » would not show
              the key, which would be absurd. The copy button returns the
              whole value anyway without revealing anything. The button
              label follows its STATE: « Masquer » once revealed, otherwise
              a screen reader announces the opposite of what it does. */}
          <StackItem size="fill">
            <Text type="code" hasTabularNumbers maxLines={revealed.has(row.key) ? 0 : 1} wordBreak="break-all">
              {revealed.has(row.key) ? row.key : `${row.key.slice(0, 10)}…${row.key.slice(-4)}`}
            </Text>
          </StackItem>
          <Button
            label={revealed.has(row.key) ? t("Masquer") : t("Afficher")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={revealed.has(row.key) ? EyeSlashIcon : EyeIcon} size="sm" />}
            onClick={() =>
              setRevealed((prev) => {
                const next = new Set(prev);
                if (next.has(row.key)) next.delete(row.key);
                else next.add(row.key);
                return next;
              })
            }
          />
          <Button
            label={t("Copier la clé")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
            onClick={() => copyKey(row.key, row.key_alias)}
          />
        </HStack>
      ),
    },
    {
      key: "last_active",
      header: t("Dernière utilisation"),
      width: proportional(4, { minWidth: 110 }),
      // Never used = useful information, not an empty cell: it is exactly the
      // key that can be revoked without breaking anything.
      renderCell: (row) => (row.last_active
        ? new Date(row.last_active).toLocaleDateString(numLocale)
        : t("Jamais utilisée")),
    },
    { key: "spend", header: t("Dépensé"), width: proportional(2, { minWidth: 80 }), renderCell: (row) => `${Math.round(row.spend || 0).toLocaleString(numLocale)} tokens` },
    {
      key: "actions" as keyof ApiKey,
      header: "",
      width: pixel(88),
      renderCell: (row) => (
        <HStack gap={1} vAlign="center">
          <Button
            label={t("Renommer")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={PencilIcon} size="sm" />}
            onClick={() => renameKey(row.key, row.key_alias)}
          />
          <Button label={t("Révoquer")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => revokeKey(row.key, row.key_alias)} />
        </HStack>
      ),
    },
  ];

  const pct = data && !data.account.unlimited && data.account.max_budget ? (data.account.spend / data.account.max_budget) * 100 : 0;

  // Two versions of the same snippet: the displayed one (key hidden if not
  // revealed) and the copied one (always the real key).
  const snippetAffiche =
    data && selectedKey && selectedModel
      ? buildSnippet(tool, data.public_api_url, selectedKey, selectedModel, data.model_limits, revealKeyInSnippet)
      : "";
  const snippetReel =
    data && selectedKey && selectedModel
      ? buildSnippet(tool, data.public_api_url, selectedKey, selectedModel, data.model_limits, true)
      : "";

  return (
    <VStack gap={5} maxWidth={980}>
      <HStack hAlign="between" vAlign="start" wrap="wrap" gap={3}>
        <VStack gap={1}>
          <Heading level={1}>{t("Mes clés API")}</Heading>
          <Text type="supporting" color="secondary">{t("Des clés personnelles pour appeler les modèles via l'API compatible OpenAI.")}</Text>
        </VStack>
        <HStack gap={2} vAlign="end">
          <TextInput
            label={t("Nom")}
            isLabelHidden
            value={keyName}
            onChange={setKeyName}
            placeholder={t("Nom (ex: mon-laptop)")}
            size="sm"
          />
          <Button label={t("Nouvelle clé")} variant="primary" size="sm" icon={<Icon icon={PlusIcon} size="sm" />} onClick={createKey} isLoading={enCours} />
        </HStack>
      </HStack>

      {erreurChargement && (
        <Card>
          <HStack hAlign="between" vAlign="center" gap={3}>
            <Text type="supporting" color="secondary">
              {t("Impossible de charger tes clés :")} {erreurChargement}
            </Text>
            <Button label={t("Réessayer")} variant="secondary" size="sm" onClick={refresh} />
          </HStack>
        </Card>
      )}

      {data && (
        <Card>
          <VStack gap={2}>
            <Text type="supporting" color="secondary">
              {t("Endpoint :")} {data.public_api_url} {t("— compatible OpenAI.")}
            </Text>
            {data.account.unlimited ? (
              <HStack>
                <Badge label={t("Budget illimité (admin)")} variant="warning" />
              </HStack>
            ) : (
              <VStack gap={1}>
                <HStack hAlign="between">
                  <Text type="supporting" color="secondary">
                    {t("Budget du compte — partagé par toutes tes clés /")} {data.budget_duration}
                  </Text>
                  <Text type="supporting" color="secondary">
                    {Math.round(data.account.spend).toLocaleString(numLocale)} / {Math.round(data.account.max_budget).toLocaleString(numLocale)} tokens
                  </Text>
                </HStack>
                <ProgressBar
                  label={t("Budget")}
                  isLabelHidden
                  value={Math.min(pct, 100)}
                  variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                />
                <HStack gap={3} vAlign="center">
                  <Text type="supporting" color="secondary">
                    {Math.max(Math.round(data.account.max_budget - data.account.spend), 0).toLocaleString(numLocale)} {t("tokens restants")}
                  </Text>
                  {data.account.has_pending ? (
                    <Badge label={t("Demande en attente")} variant="neutral" />
                  ) : (
                    <Button
                      label={t("Demander plus de tokens")}
                      variant="secondary"
                      size="sm"
                      onClick={() => setShowBudgetForm((v) => !v)}
                    />
                  )}
                </HStack>
                {showBudgetForm && (
                  <HStack gap={2}>
                    <TextInput label={t("Raison")} isLabelHidden value={budgetReason} onChange={setBudgetReason} placeholder={t("Raison (optionnel)")} size="sm" />
                    <Button label={t("Envoyer")} variant="secondary" size="sm" onClick={requestBudget} />
                  </HStack>
                )}
              </VStack>
            )}
          </VStack>
        </Card>
      )}

      {discord && (discord.linkable || discord.linked) && (
        <Card>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={1}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{t("Notifications Discord")}</Text>
                {discord.linked ? <Badge label={t("Lié")} variant="success" /> : null}
              </HStack>
              <Text type="supporting" color="secondary">
                {discord.linked
                  ? `${t("Compte Discord lié :")} ${discord.discord_name || "—"}`
                  : t("Lie ton compte Discord pour recevoir les annonces (changement de modèle, maintenance…) en message privé.")}
              </Text>
            </VStack>
            {discord.linked ? (
              <Button label={t("Délier")} variant="secondary" size="sm" onClick={unlinkDiscord} />
            ) : (
              <Button
                label={t("Lier mon compte Discord")}
                variant="primary"
                size="sm"
                onClick={() => { window.location.href = "/discord/link"; }}
              />
            )}
          </HStack>
        </Card>
      )}

      {data && data.user_keys.length === 0 && (
        <EmptyState
          icon={<Icon icon={KeyIcon} size="lg" />}
          title={t("Aucune clé pour l'instant.")}
          description={t("Utilise « Nouvelle clé » en haut à droite pour en générer une.")}
        />
      )}

      {data && data.user_keys.length > 0 && (
        <Card padding={0}>
          <Table<ApiKey & Record<string, unknown>> data={data.user_keys} columns={columns} idKey="key" density="balanced" dividers="rows" />
        </Card>
      )}

      {data && data.user_keys.length > 0 && (
        <Card>
          <VStack gap={3}>
            <Text weight="semibold">{t("Intégrations")}</Text>
            <HStack gap={3} wrap="wrap">
              <Selector label={t("Clé")} value={selectedKey} onChange={(v) => setSelectedKey(v ?? "")} options={data.user_keys.map((k) => ({ value: k.key, label: k.key_alias }))} />
              <Selector
                label={t("Modèle")}
                value={selectedModel}
                onChange={(v) => setSelectedModel(v ?? "")}
                options={[
                  ...(data.auto_model ? [{ value: data.auto_model, label: `${data.auto_model} — ${t("recommandé")}` }] : []),
                  ...data.running_models.filter((m) => m !== data.auto_model).map((m) => ({ value: m, label: m })),
                ]}
              />
            </HStack>
            {selectedModel === data.auto_model && (
              <Text type="supporting" color="secondary">
                {t("Modèle virtuel : route toujours vers le modèle chat actuellement chargé — ton code n'a rien à changer quand l'admin bascule de modèle. Choisis un modèle nommé pour t'épingler à celui-là.")}
              </Text>
            )}
            {/* The tab strip exceeds the panel width (a dozen integrations
                for ~684 px): without a scrolling container, the last tabs
                overflow out of the frame and are unreachable. */}
            <HStack width="100%" style={{ overflowX: "auto" }}>
              <TabList value={tool} onChange={(v) => setTool(v as IntegrationTool)}>
                {/* `tool`: do not shadow the `t` translation function. */}
                {INTEGRATION_TOOLS.map((tool) => (
                  <Tab key={tool.value} value={tool.value} label={t(tool.label)} />
                ))}
              </TabList>
            </HStack>
            <Button
              label={revealKeyInSnippet ? t("Masquer la clé") : t("Révéler la clé")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={revealKeyInSnippet ? EyeSlashIcon : EyeIcon} size="sm" />}
              onClick={() => setRevealKeyInSnippet((v) => !v)}
            />
            <Text type="supporting" color="secondary">
              {t("Le bouton copier donne toujours la vraie clé, même affichée masquée.")}
            </Text>
            {/* isWrapped: the <pre> is overflow-x hidden, so without line wrapping
                the long lines (URL + key) are cut with NO way to read
                or select them. */}
            {/* The key is hidden on screen (we do not want a clear-text key
                on a shared screen or a screenshot), but COPY must give the
                real key — otherwise one pastes « sk-Oo-••••83EQ » in their
                config and nothing works. CodeBlock copies its own `code`:
                so we rewrite the clipboard right after with the revealed version. */}
            <CodeBlock
              isWrapped
              code={snippetAffiche}
              onCopy={() => {
                if (!snippetReel) return;
                void copierTexte(snippetReel, () =>
                  showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }));
              }}
              language={snippetLanguage(tool)}
              width="100%"
            />
          </VStack>
        </Card>
      )}
    </VStack>
  );
}
