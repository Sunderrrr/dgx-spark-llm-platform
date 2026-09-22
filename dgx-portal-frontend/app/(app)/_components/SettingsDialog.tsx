"use client";

import { useCallback, useEffect, useState } from "react";
import { Dialog } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutPanel, LayoutHeader, LayoutFooter } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { Selector } from "@astryxdesign/core/Selector";
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
import { avatarGenere, avatarSrc } from "@/lib/avatar-genere";
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
import { authFetch, getJSON, postFormJSON } from "@/lib/api";
import { KeysContent } from "../keys/_components/KeysContent";
import { MemoryContent } from "../memory/_components/MemoryContent";
import { useThemeMode } from "../../theme-provider";
import { THEMES, type ThemeId } from "@/lib/themes";
import { useLang, useT, useLocale, type Lang } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { ActivityHeatmap, type ActivityDay } from "./ActivityHeatmap";
import { SecurityContent } from "./SecurityContent";

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
  // Fin de la période d'enveloppe LiteLLM : c'est là que `spend` repart à zéro.
  budget_reset_at: string | null;
  budget_duration: string;
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

type Section = "account" | "usage" | "keys" | "memory" | "avatar" | "appearance" | "mcp" | "skills";

const SECTIONS: { group: string; items: { id: Section; label: string; icon: typeof UserIcon }[] }[] = [
  {
    group: "Réglages du compte",
    items: [
      { id: "account", label: "Mon compte", icon: UserIcon },
      { id: "usage", label: "Usage", icon: ChartBarIcon },
      { id: "keys", label: "Clés API", icon: KeyIcon },
      { id: "memory", label: "Mémoire", icon: SparklesIcon },
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
  memory: "Mémoire",
  avatar: "Personnalisation",
  appearance: "Apparence",
  mcp: "MCP",
  skills: "Compétences",
};

// Constant dialog size, whatever the displayed section.
const DIALOG_HEIGHT = "min(86vh, 700px)";

// La locale est passée par le composant : le formatage suit la langue
// affichée et un helper hors composant ne peut pas appeler de hook.
function fmt(n: number, numLocale: string) {
  return Math.round(n).toLocaleString(numLocale);
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
  /** `null` = avatar généré depuis le pseudo (aucun logo choisi). */
  onAvatarChange?: (avatarId: string | null) => void;
  initialSection?: Section;
}) {
  const csrf = useCsrf();
  const showToast = useToast();
  const { mode, setMode, themeId, setThemeId } = useThemeMode();
  const { lang, setLang } = useLang();
  const t = useT();
  const numLocale = useLocale();
  const isNarrow = useIsNarrow();
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

  /** Envoie un formulaire de réglage et rend un verdict exploitable.
   *
   * Plusieurs actions de ce dialogue partaient « à l'aveugle » : `void
   * postForm(...)` pour le thème, la langue et l'avatar, un `await
   * postFormJSON(...)` dont on ignorait le `ok` pour l'interrupteur MCP, un
   * `postForm` sans statut pour les suppressions. Une préférence refusée restait
   * donc affichée comme appliquée jusqu'au rechargement, et une suppression
   * ratée s'annonçait comme réussie.
   */
  async function envoyerForm(url: string, corps: Record<string, string>): Promise<boolean> {
    if (!csrf) return false;
    try {
      const res = await authFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
        body: new URLSearchParams(corps).toString(),
      });
      if (res.ok) return true;
      const r = (await res.json().catch(() => ({}))) as { error?: string; code?: string };
      showToast({
        body: r.code ? t(r.code) : r.error ? t(r.error) : t("L'action a échoué."),
        type: "error",
      });
      return false;
    } catch {
      showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
      return false;
    }
  }

  async function saveMcp() {
    if (!csrf) return;
    // Avant : `return` muet. Le bouton « Enregistrer le serveur » ne faisait
    // donc RIEN tant que le nom ou l'URL manquait, sans dire lequel.
    const manquants = [!mcpForm.name.trim() && t("le nom"), !mcpForm.url.trim() && t("l'URL")]
      .filter(Boolean) as string[];
    if (manquants.length) {
      showToast({ body: `${t("Champs requis manquants :")} ${manquants.join(", ")}.`, type: "error" });
      return;
    }
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
        showToast({ body: result.error ? t(result.error) : t("Échec de la connexion au serveur MCP."), type: "error" });
      }
    } catch {
      // `postFormJSON` ne teste pas le statut et fait `res.json()` : un corps
      // non-JSON (413 du plafond de formulaire, page HTML 502, 500 gunicorn)
      // rejetait la promesse. Le `finally` rendait la main au bouton et
      // l'utilisateur ne voyait STRICTEMENT RIEN, alors que rien n'était
      // enregistré. Le motif est celui d'`envoyerForm`, juste au-dessus.
      showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
    } finally {
      setIsSaving(false);
    }
  }

  async function toggleMcp(id: number, enabled: boolean) {
    const basculer = (v: boolean) =>
      setData((prev) =>
        prev
          ? { ...prev, mcp_servers: prev.mcp_servers.map((s) => (s.id === id ? { ...s, enabled: v ? 1 : 0 } : s)) }
          : prev,
      );
    basculer(enabled);                    // application immédiate, sans attendre le réseau
    if (!(await envoyerForm("/mcp", { action: "toggle", id: String(id), enabled: enabled ? "1" : "0" }))) {
      // Le résultat était ignoré : un refus laissait l'interrupteur basculé,
      // donc l'écran affirmait un état que le serveur n'avait pas enregistré.
      basculer(!enabled);
    }
  }

  async function deleteMcp(id: number, nom: string) {
    // Suppression définitive (URL + secret) en un clic : on confirme.
    if (!window.confirm(t("Supprimer le serveur MCP « {nom} » ? Son secret sera perdu.").replace("{nom}", nom))) return;
    if (await envoyerForm("/mcp", { action: "delete", id: String(id) })) {
      showToast({ body: t("Serveur MCP supprimé."), type: "info" });
    }
    refresh();
  }

  async function saveSkill() {
    if (!csrf) return;
    const manquants = [
      !skillForm.name.trim() && t("le nom"),
      !skillForm.description.trim() && t("la description"),
      !skillForm.instructions.trim() && t("les instructions"),
    ].filter(Boolean) as string[];
    if (manquants.length) {
      showToast({ body: `${t("Champs requis manquants :")} ${manquants.join(", ")}.`, type: "error" });
      return;
    }
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
        showToast({ body: result.error ? t(result.error) : t("Échec de l'enregistrement."), type: "error" });
        return;
      }
      const wasEdit = editingSkillId !== null;
      setSkillForm({ name: "", description: "", instructions: "" });
      setIsAddingSkill(false);
      setEditingSkillId(null);
      showToast({ body: wasEdit ? t("Compétence mise à jour.") : t("Compétence enregistrée."), type: "info" });
      refresh();
    } catch {
      // Même trou que `saveMcp` : sans `catch`, un 413/502 muet laissait croire
      // à un enregistrement qui n'avait pas eu lieu.
      showToast({ body: t("Le serveur n'a pas répondu — réessaie."), type: "error" });
    } finally {
      setIsSaving(false);
    }
  }

  async function deleteSkill(id: number, nom: string) {
    if (!window.confirm(t("Supprimer la compétence « {nom} » ?").replace("{nom}", nom))) return;
    if (await envoyerForm("/skills", { action: "delete", id: String(id) })) {
      showToast({ body: t("Compétence supprimée."), type: "info" });
    }
    refresh();
  }

  async function selectTheme(id: ThemeId) {
    const precedent = themeId;
    setThemeId(id);            // applied immediately, without waiting for the network
    // …mais si le serveur refuse, on REVIENT au thème précédent : garder un
    // choix non enregistré ferait croire qu'il tient, jusqu'au rechargement.
    if (!(await envoyerForm("/settings/appearance", { theme_id: id }))) setThemeId(precedent);
  }

  async function selectLang(l: Lang) {
    const precedent = lang;
    setLang(l);
    if (!(await envoyerForm("/settings/appearance", { lang: l }))) setLang(precedent);
  }

  async function selectAvatar(avatarId: string) {
    const precedent = data?.avatar_id ?? null;
    // `""` (choix « généré ») se normalise en `null` : c'est ainsi que le reste
    // de l'application représente « aucun logo choisi », et donc l'avatar généré.
    const normalise = avatarId || null;
    setData((prev) => (prev ? { ...prev, avatar_id: normalise } : prev));
    if (await envoyerForm("/settings/avatar", { avatar_id: avatarId })) {
      onAvatarChange?.(normalise);
    } else {
      setData((prev) => (prev ? { ...prev, avatar_id: precedent } : prev));
    }
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
      // Phones: a fullscreen dialog (the fixed 980×700 window overflowed the
      // viewport). Desktop keeps the fixed size — FIXED height (not just
      // maxHeight) so the window doesn't jump when switching a tall section
      // ("My account") for a short one (empty skills). Dialog has no `height`
      // prop, hence the inline style; min() keeps it on short screens.
      variant={isNarrow ? "fullscreen" : "standard"}
      width={isNarrow ? undefined : 980}
      maxHeight={isNarrow ? undefined : DIALOG_HEIGHT}
      style={isNarrow ? undefined : { height: DIALOG_HEIGHT }}>
      <Layout
        height="fill"
        padding={0}
        start={
          isNarrow ? undefined : (
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
                <Avatar src={avatarSrc(data?.avatar_id, acct?.username || "")}
                        name={acct?.fullname || ""} size="sm" />
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
          )
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
                  {isNarrow && !(isAddingMcp || isAddingSkill) ? (
                    // No side rail on mobile → a dropdown drives the section.
                    <Selector
                      label={t("Section")}
                      isLabelHidden
                      size="sm"
                      value={section}
                      onChange={(v) => { if (v) { setSection(v as Section); leaveForm(); } }}
                      options={SECTIONS.flatMap((g) => g.items).map((it) => ({ label: t(it.label), value: it.id }))}
                    />
                  ) : (
                    <Heading level={3}>{t(paneTitle)}</Heading>
                  )}
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
                    <Avatar src={avatarSrc(data?.avatar_id, acct.username)}
                            name={acct.fullname} size="xl" />
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
                          ["Total période", fmt(act.total, numLocale)],
                          ["Pic journalier", act.peak_day ? `${new Date(act.peak_day + "T00:00:00").toLocaleDateString(numLocale)} — ${fmt(act.peak, numLocale)}` : "—"],
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
                          ["Entrée (prompt)", fmt(act.prompt, numLocale)],
                          ["Sortie (généré)", fmt(act.completion, numLocale)],
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
                              {t("Consommé sur la période")}
                            </Text>
                            <Text type="supporting" color="secondary" hasTabularNumbers>
                              {fmt(acct.spend, numLocale)} / {fmt(acct.max_budget || 0, numLocale)} tokens
                            </Text>
                          </HStack>
                          <ProgressBar
                            label={t("Budget")}
                            isLabelHidden
                            value={Math.min(pct, 100)}
                            variant={pct >= 90 ? "error" : pct >= 70 ? "warning" : "success"}
                          />
                          {/* Le compteur repart à zéro à la date de remise à
                              zéro de l'enveloppe LiteLLM (hebdomadaire par
                              défaut). L'afficher évite de croire à un quota
                              quotidien qui ne remonte jamais. */}
                          {acct.budget_reset_at ? (
                            <Text type="supporting" color="secondary">
                              {t("Remis à zéro le {date}.").replace(
                                "{date}", new Date(acct.budget_reset_at).toLocaleDateString(numLocale),
                              )}
                            </Text>
                          ) : null}
                        </VStack>
                      )}
                    </Card>
                  </VStack>

                  <SecurityContent />
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
                                label={l.unlimited ? t("Illimité") : `${fmt(l.max ?? 0, numLocale)} ${l.unit}`}
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
              {section === "memory" && <MemoryContent />}

              {/* ── Personalization ───────────────────────────────────── */}
              {section === "avatar" && (
                <VStack gap={3}>
                  <Text type="supporting" color="secondary">
                    {t("Par défaut, ton avatar est créé à partir de ton pseudo. Tu peux aussi choisir un logo de marque d'IA — pas d'import d'image personnelle.")}
                  </Text>
                  <Grid columns={{ minWidth: 110, max: 5 }} gap={3}>
                    {/* Attend les réglages : avant, le pseudo est inconnu et le
                        monogramme afficherait « ? » le temps de la requête. */}
                    {data && (
                      <SelectableCard
                        key="genere"
                        label={t("Généré depuis mon pseudo")}
                        isSelected={!data.avatar_id}
                        onChange={() => selectAvatar("")}
                        padding={3}>
                        <VStack gap={2} hAlign="center">
                          <Avatar src={avatarGenere(data.account?.username || "")} name={acct?.fullname || ""} size="lg" />
                          <Text type="supporting" color="secondary">
                            {t("Généré depuis mon pseudo")}
                          </Text>
                        </VStack>
                      </SelectableCard>
                    )}
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
                                  onClick={() => deleteMcp(s.id, s.name)}
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
                          // Un jeton Bearer se masque comme un mot de passe : il
                          // restait lisible en clair pendant la saisie (partage
                          // d'écran, capture). Il n'est jamais re-servi par le
                          // serveur (`/api/settings` n'expose que `has_auth`).
                          type="password"
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
                                onClick={() => deleteSkill(s.id, s.name)}
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
