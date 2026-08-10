"use client";

import { useCallback, useEffect, useState } from "react";
import { Dialog } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutPanel, LayoutHeader, LayoutFooter } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Switch } from "@astryxdesign/core/Switch";
import { Divider } from "@astryxdesign/core/Divider";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { SelectableCard } from "@astryxdesign/core/SelectableCard";
import { Grid } from "@astryxdesign/core/Grid";
import { Avatar } from "@astryxdesign/core/Avatar";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { useToast } from "@astryxdesign/core/Toast";
import {
  KeyIcon,
  PlusIcon,
  TrashIcon,
  PencilSquareIcon,
  ServerStackIcon,
  SparklesIcon,
  UserCircleIcon,
  UserIcon,
  ChartBarIcon,
  SwatchIcon,
  ArrowPathIcon,
  SunIcon,
  MoonIcon,
  ComputerDesktopIcon,
  ArrowLeftIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, postForm, postFormJSON } from "@/lib/api";
import { KeysContent } from "../keys/_components/KeysContent";
import { useThemeMode } from "../../theme-provider";
import { THEMES, type ThemeId } from "@/lib/themes";
import { useLang, useT, type Lang } from "@/lib/i18n";
import { ActivityHeatmap, type ActivityDay } from "./ActivityHeatmap";

type McpServer = {
  id: number;
  name: string;
  url: string;
  description: string;
  allowed_tools: string;
  enabled: number;
  has_auth: number;
  created_at: string;
};
type Skill = { id: number; name: string; description: string; instructions: string; created_at: string };
type AvatarChoice = { id: string; label: string };
type Account = {
  username: string;
  fullname: string;
  is_admin: boolean;
  spend: number;
  max_budget: number | null;
  unlimited: boolean;
  key_count: number;
  mcp_count: number;
  skill_count: number;
};
type Activity = {
  days: ActivityDay[];
  total: number;
  prompt: number;
  completion: number;
  peak: number;
  peak_day: string | null;
  active_days: number;
  avg: number;
};
type Limit = {
  key: string;
  label: string;
  desc: string;
  used: number | null;
  max: number | null;
  unit: string;
  unlimited: boolean;
};
type SettingsData = {
  activity: Activity;
  limits: Limit[];
  mcp_servers: McpServer[];
  skills: Skill[];
  avatar_id: string | null;
  avatars: AvatarChoice[];
  account: Account;
};

type Section = "account" | "usage" | "keys" | "avatar" | "appearance" | "mcp" | "skills";

const SECTIONS: { group: string; items: { id: Section; label: string; icon: typeof UserIcon }[] }[] = [
  {
    group: "Réglages du compte",
    items: [
      { id: "account", label: "Mon compte", icon: UserIcon },
      { id: "usage", label: "Usage", icon: ChartBarIcon },
      { id: "keys", label: "Clés API", icon: KeyIcon },
    ],
  },
  {
    group: "Réglages de l'app",
    items: [
      { id: "avatar", label: "Personnalisation", icon: UserCircleIcon },
      { id: "appearance", label: "Apparence", icon: SwatchIcon },
      { id: "mcp", label: "MCP", icon: ServerStackIcon },
      { id: "skills", label: "Compétences", icon: SparklesIcon },
    ],
  },
];

const SECTION_TITLES: Record<Section, string> = {
  account: "Mon compte",
  usage: "Usage",
  keys: "Clés API",
  avatar: "Personnalisation",
  appearance: "Apparence",
  mcp: "MCP",
  skills: "Compétences",
};

// Constant dialog size, whatever the displayed section.
const DIALOG_HEIGHT = "min(86vh, 700px)";

function fmt(n: number) {
  return Math.round(n).toLocaleString("fr-FR");
}

/** 12,400 → "12 k": the top tiles must stay readable. */
function compact(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1).replace(/\.0$/, "")} G`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")} M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} k`;
  return String(Math.round(n));
}

const EMPTY_ACTIVITY: Activity = {
  days: [], total: 0, prompt: 0, completion: 0,
  peak: 0, peak_day: null, active_days: 0, avg: 0,
};

export function SettingsDialog({
  isOpen,
  onOpenChange,
  onAvatarChange,
  initialSection,
}: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onAvatarChange?: (avatarId: string) => void;
  initialSection?: Section;
}) {
  const csrf = useCsrf();
  const showToast = useToast();
  const { mode, setMode, themeId, setThemeId } = useThemeMode();
  const { lang, setLang } = useLang();
  const t = useT();
  const [section, setSection] = useState<Section>("account");
  // When opening with a requested section (e.g. "keys" from the home page),
  // we go straight to that tab. The gear opens without a section →
  // we keep the last tab visited.
  useEffect(() => {
    // One-off sync on open (not a render cascade): we set the requested
    // tab. Legitimate "sync from a prop" use.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (isOpen && initialSection) setSection(initialSection);
  }, [isOpen, initialSection]);
  const [data, setData] = useState<SettingsData | null>(null);
  // "Form" sub-page of a section (reference design pattern: the list
  // gives way to a full-frame form with a back arrow).
  const [isAddingMcp, setIsAddingMcp] = useState(false);
  const [isAddingSkill, setIsAddingSkill] = useState(false);
  // id of the edited entry, or null when the form is used to create one.
  const [editingMcpId, setEditingMcpId] = useState<number | null>(null);
  const [editingSkillId, setEditingSkillId] = useState<number | null>(null);

  const [mcpForm, setMcpForm] = useState({ name: "", url: "", description: "", allowedTools: "", auth: "" });
  const [skillForm, setSkillForm] = useState({ name: "", description: "", instructions: "" });
  const [isSaving, setIsSaving] = useState(false);

  const refresh = useCallback(() => {
    getJSON<SettingsData>("/api/settings").then(setData).catch(() => {});
  }, []);

  // Loaded on open (and not on mount): the dialog lives in the layout,
  // so it's permanently mounted — without this we'd make an API call on every page.
  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

  function leaveForm() {
    setIsAddingMcp(false);
    setIsAddingSkill(false);
    setEditingMcpId(null);
    setEditingSkillId(null);
  }

  function closeAll() {
    leaveForm();
    onOpenChange(false);
  }

  async function saveMcp() {
    if (!csrf || !mcpForm.name.trim() || !mcpForm.url.trim()) return;
    setIsSaving(true);
    try {
      const result = await postFormJSON<{ ok: boolean; error?: string; tool_count?: number }>("/mcp", csrf, {
        action: editingMcpId ? "update" : "create",
        ...(editingMcpId ? { id: String(editingMcpId) } : {}),
        name: mcpForm.name,
        url: mcpForm.url,
        description: mcpForm.description,
        allowed_tools: mcpForm.allowedTools,
        auth_header: mcpForm.auth,
      });
      if (result.ok) {
        const wasEdit = editingMcpId !== null;
        setMcpForm({ name: "", url: "", description: "", allowedTools: "", auth: "" });
        setIsAddingMcp(false);
        setEditingMcpId(null);
        showToast({
          body: wasEdit
            ? `${t("Serveur MCP mis à jour")} (${result.tool_count ?? 0} ${t("outil(s) trouvé(s)")}).`
            : `${t("Serveur MCP connecté")} (${result.tool_count ?? 0} ${t("outil(s) trouvé(s)")}).`,
          type: "info",
        });
        refresh();
      } else {
        showToast({ body: result.error || t("Échec de la connexion au serveur MCP."), type: "error" });
      }
    } finally {
      setIsSaving(false);
    }
  }

  async function toggleMcp(id: number, enabled: boolean) {
    if (!csrf) return;
    setData((prev) =>
      prev
        ? { ...prev, mcp_servers: prev.mcp_servers.map((s) => (s.id === id ? { ...s, enabled: enabled ? 1 : 0 } : s)) }
        : prev,
    );
    await postFormJSON("/mcp", csrf, { action: "toggle", id: String(id), enabled: enabled ? "1" : "0" });
  }

  async function deleteMcp(id: number) {
    if (!csrf) return;
    await postForm("/mcp", csrf, { action: "delete", id: String(id) });
    showToast({ body: t("Serveur MCP supprimé."), type: "info" });
    refresh();
  }

  async function saveSkill() {
    if (!csrf || !skillForm.name.trim() || !skillForm.description.trim() || !skillForm.instructions.trim()) return;
    setIsSaving(true);
    try {
      const result = await postFormJSON("/skills", csrf, {
        action: editingSkillId ? "update" : "create",
        ...(editingSkillId ? { id: String(editingSkillId) } : {}),
        name: skillForm.name,
        description: skillForm.description,
        instructions: skillForm.instructions,
      });
      if (!result.ok) {
        showToast({ body: result.error || t("Échec de l'enregistrement."), type: "error" });
        return;
      }
      const wasEdit = editingSkillId !== null;
      setSkillForm({ name: "", description: "", instructions: "" });
      setIsAddingSkill(false);
      setEditingSkillId(null);
      showToast({ body: wasEdit ? t("Compétence mise à jour.") : t("Compétence enregistrée."), type: "info" });
      refresh();
    } finally {
      setIsSaving(false);
    }
  }

  async function deleteSkill(id: number) {
    if (!csrf) return;
    await postForm("/skills", csrf, { action: "delete", id: String(id) });
    showToast({ body: t("Compétence supprimée."), type: "info" });
    refresh();
  }

  async function selectTheme(id: ThemeId) {
    setThemeId(id);            // applied immediately, without waiting for the network
    if (csrf) void postForm("/settings/appearance", csrf, { theme_id: id });
  }

  async function selectLang(l: Lang) {
    setLang(l);
    if (csrf) void postForm("/settings/appearance", csrf, { lang: l });
  }

  async function selectAvatar(avatarId: string) {
    if (!csrf) return;
    await postForm("/settings/avatar", csrf, { avatar_id: avatarId });
    setData((prev) => (prev ? { ...prev, avatar_id: avatarId } : prev));
    onAvatarChange?.(avatarId);
  }

  const acct = data?.account;
  const act = data?.activity ?? EMPTY_ACTIVITY;
  const limits = data?.limits ?? [];
  const pct = acct && !acct.unlimited && acct.max_budget ? (acct.spend / acct.max_budget) * 100 : 0;

  // Right pane title: section name, or that of the open sub-page.
  const paneTitle = isAddingMcp ? "MCP" : isAddingSkill ? "Compétences" : SECTION_TITLES[section];

  return (
    <Dialog
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      purpose="form"
      width={980}
      maxHeight={DIALOG_HEIGHT}
      // FIXED height (and not just maxHeight): without this the dialog
      // resizes on every section change — "My account" is tall,
      // an empty skills list is short — and the window jumps under the
      // cursor. Dialog doesn't expose a `height` prop, hence the inline style;
      // min() keeps the whole window on short screens.
      style={{ height: DIALOG_HEIGHT }}>
      <Layout
        height="fill"
        padding={0}
        start={
          <LayoutPanel width={232} hasDivider role="navigation">
            <VStack height="100%" hAlign="stretch">
              <HStack padding={4} gap={2} vAlign="center">
                <Icon icon={SparklesIcon} size="sm" color="accent" />
                <Text weight="bold">Cronos</Text>
              </HStack>
              <VStack gap={4} paddingInline={2} height="100%">
                {SECTIONS.map((grp) => (
                  <VStack key={grp.group} gap={1}>
                    <HStack paddingInline={2}>
                      <Text type="supporting" color="secondary">{t(grp.group)}</Text>
                    </HStack>
                    <List>
                      {grp.items.map((it) => (
                        <ListItem
                          key={it.id}
                          label={t(it.label)}
                          startContent={<Icon icon={it.icon} size="sm" color="secondary" />}
                          isSelected={section === it.id && !isAddingMcp && !isAddingSkill}
                          onClick={() => {
                            setSection(it.id);
                            setIsAddingMcp(false);
                            setIsAddingSkill(false);
                          }}
                        />
                      ))}
                    </List>
                  </VStack>
                ))}
              </VStack>
              <Divider />
              <HStack padding={3} gap={2} vAlign="center">
                {data?.avatar_id ? (
                  <Avatar src={`/avatars/${data.avatar_id}.svg`} name={acct?.fullname || ""} size="sm" />
                ) : (
                  <Avatar name={acct?.fullname || ""} size="sm" />
                )}
                <VStack gap={0}>
                  <Text type="supporting" weight="semibold" maxLines={1}>
                    {acct?.fullname || ""}
                  </Text>
                  <Text type="supporting" color="secondary" maxLines={1}>
                    {acct?.username || ""}
                  </Text>
                </VStack>
              </HStack>
            </VStack>
          </LayoutPanel>
        }
        content={
          <LayoutContent padding={0} isScrollable={false}>
            <Layout
              height="fill"
              header={
            <LayoutHeader hasDivider>
              {/* paddingInline aligned with the padding={5} of the content below:
                  otherwise the panel title ("My account"…) was stuck to the
                  left edge while all the content was indented. */}
              <HStack hAlign="between" vAlign="center" gap={3} paddingInline={5} paddingBlock={3}>
                <HStack gap={2} vAlign="center">
                  {(isAddingMcp || isAddingSkill) && (
                    <Button
                      label={t("Retour")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={ArrowLeftIcon} size="sm" />}
                      onClick={leaveForm}
                    />
                  )}
                  <Heading level={3}>{t(paneTitle)}</Heading>
                </HStack>
                <Button
                  label={t("Fermer")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={XMarkIcon} size="sm" />}
                  onClick={closeAll}
                />
              </HStack>
            </LayoutHeader>
              }
              content={
            <LayoutContent padding={5} isScrollable>
              {/* ── My account ─────────────────────────────────────────── */}
              {section === "account" && acct && (
                <VStack gap={5}>
                  <VStack gap={2} hAlign="center">
                    {data?.avatar_id ? (
                      <Avatar src={`/avatars/${data.avatar_id}.svg`} name={acct.fullname} size="xl" />
                    ) : (
                      <Avatar name={acct.fullname} size="xl" />
                    )}
                    <Heading level={2}>{acct.fullname}</Heading>
                    <HStack gap={2} vAlign="center">
                      <Text type="supporting" color="secondary">
                        {acct.username}
                      </Text>
                      {acct.is_admin && <Badge label="Admin" variant="warning" />}
                    </HStack>
                  </VStack>

                  <Card padding={0}>
                    <Grid columns={4}>
                      {[
                        { v: compact(act.total), l: "Tokens totaux" },
                        { v: compact(act.peak), l: "Pic journalier" },
                        { v: String(act.active_days), l: "Jours actifs" },
                        { v: compact(act.avg), l: "Moyenne / jour" },
                      ].map((s) => (
                        <VStack key={s.l} gap={0} hAlign="center" padding={4}>
                          <Text size="xl" weight="bold" hasTabularNumbers>
                            {s.v}
                          </Text>
                          <Text type="supporting" color="secondary">
                            {t(s.l)}
                          </Text>
                        </VStack>
                      ))}
                    </Grid>
                  </Card>

                  <VStack gap={2}>
                    <HStack hAlign="between" vAlign="center">
                      <Text type="supporting" color="secondary">{t("ACTIVITÉ TOKENS")}</Text>
                      <Text type="supporting" color="secondary">{t("6 derniers mois")}</Text>
                    </HStack>
                    <Card>
                      <ActivityHeatmap days={act.days} />
                    </Card>
                  </VStack>

                  <Grid columns={2} gap={4}>
                    <VStack gap={2}>
                      <Text weight="semibold">{t("Insights d'activité")}</Text>
                      <VStack gap={1}>
                        {[
                          ["Total période", fmt(act.total)],
                          ["Pic journalier", act.peak_day ? `${new Date(act.peak_day + "T00:00:00").toLocaleDateString("fr-FR")} — ${fmt(act.peak)}` : "—"],
                          ["Jours actifs", String(act.active_days)],
                        ].map(([l, v]) => (
                          <HStack key={l} hAlign="between" gap={3}>
                            <Text type="supporting" color="secondary">{t(l)}</Text>
                            <Text type="supporting" hasTabularNumbers>{v}</Text>
                          </HStack>
                        ))}
                      </VStack>
                    </VStack>
                    <VStack gap={2}>
                      <Text weight="semibold">{t("Répartition tokens")}</Text>
                      <VStack gap={1}>
                        {[
                          ["Entrée (prompt)", fmt(act.prompt)],
                          ["Sortie (généré)", fmt(act.completion)],
                          ["Clés API actives", String(acct.key_count)],
                        ].map(([l, v]) => (
                          <HStack key={l} hAlign="between" gap={3}>
                            <Text type="supporting" color="secondary">{t(l)}</Text>
                            <Text type="supporting" hasTabularNumbers>{v}</Text>
                          </HStack>
                        ))}
                      </VStack>
                    </VStack>
                  </Grid>

                  <VStack gap={2}>
                    <Text weight="semibold">{t("Budget")}</Text>
                    <Card>
                      {acct.unlimited ? (
                        <HStack>
                          <Badge label={t("Budget illimité (admin)")} variant="warning" />
                        </HStack>
                      ) : (
                        <VStack gap={2}>
                          <HStack hAlign="between">
                            <Text type="supporting" color="secondary">
                              {t("Consommé aujourd'hui")}
                            </Text>
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {fmt(acct.spend)} / {fmt(acct.max_budget || 0)} tokens
                            </Text>
                          </HStack>
                          <ProgressBar
                            label={t("Budget")}
                            isLabelHidden
                            value={Math.min(pct, 100)}
                            variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                          />
                        </VStack>
                      )}
                    </Card>
                  </VStack>
                </VStack>
              )}

              {/* ── Usage ──────────────────────────────────────────────── */}
              {section === "usage" && (
                <VStack gap={4}>
                  <HStack hAlign="between" vAlign="start" gap={3}>
                    <VStack gap={0}>
                      <Text weight="semibold">{t("Vos limites d'utilisation")}</Text>
                      <Text type="supporting" color="secondary">{t("Suivez la consommation de votre compte sur chaque quota disponible.")}</Text>
                    </VStack>
                    <Button
                      label={t("Rafraîchir")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                      onClick={refresh}
                    />
                  </HStack>
                  <VStack gap={4}>
                    {limits.map((l) => {
                      const pourcent =
                        l.unlimited || !l.max || l.used === null
                          ? null
                          : Math.min(100, Math.round((l.used / l.max) * 100));
                      return (
                        <HStack key={l.key} gap={4} vAlign="center" hAlign="between">
                          <VStack gap={0} width="45%">
                            <Text weight="semibold">{t(l.label)}</Text>
                            <Text type="supporting" color="secondary">
                              {t(l.desc)}
                            </Text>
                          </VStack>
                          <HStack gap={3} vAlign="center" width="50%">
                            {pourcent === null ? (
                              <Badge
                                label={l.unlimited ? t("Illimité") : `${fmt(l.max ?? 0)} ${l.unit}`}
                                variant={l.unlimited ? "warning" : "neutral"}
                              />
                            ) : (
                              <>
                                <ProgressBar
                                  label={t(l.label)}
                                  isLabelHidden
                                  value={pourcent}
                                  variant={pourcent >= 90 ? "error" : pourcent >= 70 ? "warning" : "success"}
                                />
                                <Text type="supporting" color="secondary" hasTabularNumbers>
                                  {pourcent} {t("% utilisé")}
                                </Text>
                              </>
                            )}
                          </HStack>
                        </HStack>
                      );
                    })}
                  </VStack>
                  <Divider />
                  <Text type="supporting" color="secondary">
                    {t("Besoin d'augmenter tes limites ? Demande plus de tokens depuis l'onglet « Clés API », ou passe par l'assistant Support.")}
                  </Text>
                </VStack>
              )}

              {/* ── Appearance ──────────────────────────────────────────── */}
              {section === "appearance" && (
                <VStack gap={5}>
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">{t("THÈME")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("Ajuste l'apparence de l'interface.")}
                    </Text>
                    <Grid columns={3} gap={3}>
                      {[
                        { id: "light", label: "Clair", icon: SunIcon },
                        { id: "dark", label: "Sombre", icon: MoonIcon },
                        { id: "system", label: "Système", icon: ComputerDesktopIcon },
                      ].map((opt) => (
                        <SelectableCard
                          key={opt.id}
                          label={t(opt.label)}
                          isSelected={mode === opt.id}
                          onChange={() => setMode(opt.id as "light" | "dark" | "system")}
                          padding={3}>
                          <VStack gap={2} hAlign="center">
                            <Icon icon={opt.icon} size="md" color="secondary" />
                            <Text weight="semibold">{t(opt.label)}</Text>
                          </VStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">
                      {t("COULEUR D'ACCENT")}
                    </Text>
                    <Text type="supporting" color="secondary">
                      {t("Change la couleur principale de l'interface.")}
                    </Text>
                    <Grid columns={{ minWidth: 92, max: 5 }} gap={3}>
                      {THEMES.map((th) => (
                        <SelectableCard
                          key={th.id}
                          label={t(th.label)}
                          isSelected={themeId === th.id}
                          onChange={() => selectTheme(th.id)}
                          padding={3}>
                          <VStack gap={2} hAlign="center">
                            {/* Preview swatch: the only place where a raw
                                color is legitimate — it's the sample
                                itself, not a themed interface element. */}
                            <span
                              aria-hidden="true"
                              style={{
                                width: 28,
                                height: 28,
                                borderRadius: "50%",
                                background: th.swatch,
                                border: "1px solid var(--color-border)",
                              }}
                            />
                            <Text type="supporting" color="secondary">
                              {t(th.label)}
                            </Text>
                          </VStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>

                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">
                      {t("LANGUE")}
                    </Text>
                    <Text type="supporting" color="secondary">
                      {t("Choisis la langue de l'interface.")}
                    </Text>
                    <Grid columns={2} gap={3}>
                      {([
                        { id: "fr", label: t("Français"), drapeau: "🇫🇷" },
                        { id: "en", label: t("Anglais"), drapeau: "🇬🇧" },
                      ] as const).map((l) => (
                        <SelectableCard
                          key={l.id}
                          label={l.label}
                          isSelected={lang === l.id}
                          onChange={() => selectLang(l.id)}
                          padding={3}>
                          <HStack gap={2} vAlign="center">
                            <Text>{l.drapeau}</Text>
                            <Text weight="semibold">{l.label}</Text>
                          </HStack>
                        </SelectableCard>
                      ))}
                    </Grid>
                  </VStack>
                </VStack>
              )}

              {/* ── API keys ───────────────────────────────────────────── */}
              {section === "keys" && <KeysContent />}

              {/* ── Personalization ───────────────────────────────────── */}
              {section === "avatar" && (
                <VStack gap={3}>
                  <Text type="supporting" color="secondary">
                    {t("Choisis un avatar parmi les logos proposés — pas d'import d'image personnelle.")}
                  </Text>
                  <Grid columns={{ minWidth: 110, max: 5 }} gap={3}>
                    {data?.avatars.map((a) => (
                      <SelectableCard
                        key={a.id}
                        label={a.label}
                        isSelected={data.avatar_id === a.id}
                        onChange={() => selectAvatar(a.id)}
                        padding={3}>
                        <VStack gap={2} hAlign="center">
                          <Avatar src={`/avatars/${a.id}.svg`} name={a.label} size="lg" />
                          <Text type="supporting" color="secondary">
                            {a.label}
                          </Text>
                        </VStack>
                      </SelectableCard>
                    ))}
                  </Grid>
                </VStack>
              )}

              {/* ── MCP: list ────────────────────────────────────────── */}
              {section === "mcp" && !isAddingMcp && (
                <VStack gap={4}>
                  <HStack hAlign="between" vAlign="center" gap={3}>
                    <Text type="supporting" color="secondary">
                      {t("Connecte un serveur MCP distant en HTTPS : ses outils deviennent utilisables par l'assistant Support.")}
                    </Text>
                    <Button
                      label={t("Connecter un MCP")}
                      variant="primary"
                      size="sm"
                      icon={<Icon icon={PlusIcon} size="sm" />}
                      onClick={() => {
                        setEditingMcpId(null);
                        setMcpForm({ name: "", url: "", description: "", allowedTools: "", auth: "" });
                        setIsAddingMcp(true);
                      }}
                    />
                  </HStack>
                  {data && data.mcp_servers.length === 0 ? (
                    <EmptyState
                      icon={<Icon icon={ServerStackIcon} size="lg" />}
                      title={t("Aucun serveur MCP connecté.")}
                      description={t("Connecte un serveur pour étendre les capacités de l'assistant.")}
                    />
                  ) : (
                    <VStack gap={3}>
                      {data?.mcp_servers.map((s) => (
                        <Card key={s.id}>
                          <VStack gap={2}>
                            <HStack hAlign="between" vAlign="start" gap={3}>
                              <VStack gap={0}>
                                <HStack gap={2} vAlign="center">
                                  <Text weight="semibold">{s.name}</Text>
                                  {s.has_auth ? <Badge label={t("Auth")} variant="success" /> : null}
                                </HStack>
                                <Text type="supporting" color="secondary" wordBreak="break-all">
                                  {s.url}
                                </Text>
                              </VStack>
                              <HStack gap={2} vAlign="center">
                                <Switch
                                  label={t("Serveur activé")}
                                  isLabelHidden
                                  value={!!s.enabled}
                                  onChange={(v) => toggleMcp(s.id, v)}
                                />
                                <Button
                                  label={t("Modifier")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={PencilSquareIcon} size="sm" />}
                                  onClick={() => {
                                    setEditingMcpId(s.id);
                                    setMcpForm({
                                      name: s.name,
                                      url: s.url,
                                      description: s.description || "",
                                      allowedTools: s.allowed_tools || "",
                                      auth: "",
                                    });
                                    setIsAddingMcp(true);
                                  }}
                                />
                                <Button
                                  label={t("Supprimer")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={TrashIcon} size="sm" />}
                                  onClick={() => deleteMcp(s.id)}
                                />
                              </HStack>
                            </HStack>
                            {s.description && (
                              <Text type="supporting" color="secondary">
                                {s.description}
                              </Text>
                            )}
                            {s.allowed_tools && (
                              <Text type="supporting" color="secondary">
                                {t("Outils autorisés :")} {s.allowed_tools}
                              </Text>
                            )}
                          </VStack>
                        </Card>
                      ))}
                    </VStack>
                  )}
                </VStack>
              )}

              {/* ── MCP: form ───────────────────────────────────── */}
              {section === "mcp" && isAddingMcp && (
                <VStack gap={4}>
                  <VStack gap={0}>
                    <Text weight="semibold">{editingMcpId ? t("Modifier le serveur MCP") : t("Connecter un MCP personnalisé")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("Configurez la connexion et la façon dont ses outils peuvent être utilisés.")}
                    </Text>
                  </VStack>
                  <Card>
                    <VStack gap={4}>
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Nom")}
                          value={mcpForm.name}
                          onChange={(v) => setMcpForm((f) => ({ ...f, name: v }))}
                          placeholder={t("Exemple : notion_workspace")}
                          description={t("Lettres, chiffres, underscores et tirets uniquement.")}
                        />
                        <TextInput
                          label={t("URL du serveur")}
                          value={mcpForm.url}
                          onChange={(v) => setMcpForm((f) => ({ ...f, url: v }))}
                          placeholder="https://mcp.example.com/sse"
                        />
                      </Grid>
                      <TextArea
                        label={t("Description (optionnel)")}
                        value={mcpForm.description}
                        onChange={(v) => setMcpForm((f) => ({ ...f, description: v }))}
                        placeholder={t("Ce que fournit ce serveur")}
                        rows={2}
                      />
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Outils autorisés (optionnel)")}
                          value={mcpForm.allowedTools}
                          onChange={(v) => setMcpForm((f) => ({ ...f, allowedTools: v }))}
                          placeholder="search, create_page, …"
                          description={t("Séparez par des virgules. Vide = tous les outils.")}
                        />
                        <TextInput
                          label={t("Autorisation (optionnel)")}
                          value={mcpForm.auth}
                          onChange={(v) => setMcpForm((f) => ({ ...f, auth: v }))}
                          placeholder={t("Bearer token ou secret")}
                          description={
                            editingMcpId
                              ? t("Laisser vide pour conserver le secret actuel ; « - » pour le retirer.")
                              : t("Envoyé en en-tête Authorization.")
                          }
                        />
                      </Grid>
                    </VStack>
                  </Card>
                </VStack>
              )}

              {/* ── Skills: list ────────────────────────────────── */}
              {section === "skills" && !isAddingSkill && (
                <VStack gap={4}>
                  <HStack hAlign="between" vAlign="center" gap={3}>
                    <Text type="supporting" color="secondary">
                      {t("Des instructions réutilisables que tu écris toi-même ; l'assistant les charge quand elles sont utiles à ta demande.")}
                    </Text>
                    <Button
                      label={t("Nouvelle compétence")}
                      variant="primary"
                      size="sm"
                      icon={<Icon icon={PlusIcon} size="sm" />}
                      onClick={() => {
                        setEditingSkillId(null);
                        setSkillForm({ name: "", description: "", instructions: "" });
                        setIsAddingSkill(true);
                      }}
                    />
                  </HStack>
                  {data && data.skills.length === 0 ? (
                    <EmptyState
                      icon={<Icon icon={SparklesIcon} size="lg" />}
                      title={t("Aucune compétence pour l'instant.")}
                      description={t("Crée une compétence pour guider l'assistant sur une tâche récurrente.")}
                    />
                  ) : (
                    <VStack gap={3}>
                      {data?.skills.map((s) => (
                        <Card key={s.id}>
                          <HStack hAlign="between" vAlign="start" gap={3}>
                            <VStack gap={0}>
                              <Text weight="semibold">{s.name}</Text>
                              <Text type="supporting" color="secondary">
                                {s.description}
                              </Text>
                            </VStack>
                            <HStack gap={1}>
                              <Button
                                label={t("Modifier")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={PencilSquareIcon} size="sm" />}
                                onClick={() => {
                                  setEditingSkillId(s.id);
                                  setSkillForm({
                                    name: s.name,
                                    description: s.description,
                                    instructions: s.instructions || "",
                                  });
                                  setIsAddingSkill(true);
                                }}
                              />
                              <Button
                                label={t("Supprimer")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={TrashIcon} size="sm" />}
                                onClick={() => deleteSkill(s.id)}
                              />
                            </HStack>
                          </HStack>
                        </Card>
                      ))}
                    </VStack>
                  )}
                </VStack>
              )}

              {/* ── Skills: form ───────────────────────────── */}
              {section === "skills" && isAddingSkill && (
                <VStack gap={4}>
                  <VStack gap={0}>
                    <Text weight="semibold">{editingSkillId ? t("Modifier la compétence") : t("Créer une compétence")}</Text>
                    <Text type="supporting" color="secondary">
                      {t("L'assistant chargera ces instructions en contexte quand la compétence s'applique.")}
                    </Text>
                  </VStack>
                  <Card>
                    <VStack gap={4}>
                      <Grid columns={2} gap={4}>
                        <TextInput
                          label={t("Nom")}
                          value={skillForm.name}
                          onChange={(v) => setSkillForm((f) => ({ ...f, name: v }))}
                          placeholder={t("Exemple : analyse-de-logs")}
                        />
                        <TextInput
                          label={t("Description")}
                          value={skillForm.description}
                          onChange={(v) => setSkillForm((f) => ({ ...f, description: v }))}
                          placeholder={t("Quand l'utiliser, en une phrase")}
                        />
                      </Grid>
                      <TextArea
                        label={t("Instructions")}
                        value={skillForm.instructions}
                        onChange={(v) => setSkillForm((f) => ({ ...f, instructions: v }))}
                        placeholder={t("Instructions détaillées que l'assistant chargera en contexte…")}
                        rows={10}
                      />
                    </VStack>
                  </Card>
                </VStack>
              )}
            </LayoutContent>
              }
              footer={
            isAddingMcp || isAddingSkill ? (
              <LayoutFooter hasDivider>
                <HStack gap={2} hAlign="end">
                  <Button
                    label={t("Annuler")}
                    variant="secondary"
                    onClick={leaveForm}
                  />
                  <Button
                    label={isAddingMcp
                      ? editingMcpId ? t("Mettre à jour le serveur") : t("Enregistrer le serveur")
                      : editingSkillId ? t("Mettre à jour la compétence") : t("Enregistrer la compétence")}
                    variant="primary"
                    isLoading={isSaving}
                    onClick={isAddingMcp ? saveMcp : saveSkill}
                  />
                </HStack>
              </LayoutFooter>
            ) : undefined
              }
            />
          </LayoutContent>
        }
      />
    </Dialog>
  );
}
