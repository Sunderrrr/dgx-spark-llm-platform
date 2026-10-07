"use client";

import { useCallback, useEffect, useState } from "react";
import { Dialog } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutPanel, LayoutHeader, LayoutFooter } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { Selector } from "@astryxdesign/core/Selector";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Divider } from "@astryxdesign/core/Divider";
import { UserAvatar } from "@/lib/user-avatar";
import { useToast } from "@astryxdesign/core/Toast";
import {
  KeyIcon,
  ServerStackIcon,
  SparklesIcon,
  UserCircleIcon,
  UserIcon,
  ChartBarIcon,
  SwatchIcon,
  ArrowLeftIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { authFetch, getJSON, postFormJSON } from "@/lib/api";
import { KeysContent } from "../keys/_components/KeysContent";
import { MemoryContent } from "../memory/_components/MemoryContent";
import { useThemeMode } from "../../theme-provider";
import { useLang, useT, type Lang } from "@/lib/i18n";
import { type ThemeId } from "@/lib/themes";
import { useIsNarrow } from "@/lib/useIsNarrow";

import { BASE_SKILLS, loadCustomSkills, type Skill as SkillPlayground } from "@/lib/skills";
import { EMPTY_ACTIVITY, type Section, type SettingsData } from "./settings/types";
import { AccountSection } from "./settings/AccountSection";
import { UsageSection } from "./settings/UsageSection";
import { AppearanceSection } from "./settings/AppearanceSection";
import { AvatarSection } from "./settings/AvatarSection";
import { McpSection } from "./settings/McpSection";
import { SkillsSection } from "./settings/SkillsSection";

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


export function SettingsDialog({
  isOpen,
  onOpenChange,
  onAvatarChange,
  initialSection,
}: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` = avatar generated from the username (no logo chosen). */
  onAvatarChange?: (avatarId: string | null) => void;
  initialSection?: Section;
}) {
  const csrf = useCsrf();
  const showToast = useToast();
  const { mode, setMode, themeId, setThemeId } = useThemeMode();
  const { lang, setLang } = useLang();
  const t = useT();
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

  /** Sends a settings form and returns an actionable verdict.
   *
   * Several actions of this dialog went out « blindly »: `void
   * postForm(...)` for the theme, the language and the avatar, an `await
   * postFormJSON(...)` whose `ok` was ignored for the MCP toggle, a
   * `postForm` without status for the deletions. A refused preference thus
   * stayed displayed as applied until reload, and a failed deletion
   * announced itself as successful.
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
    // Before: silent `return`. The « Enregistrer le serveur » button thus did
    // NOTHING while the name or URL was missing, without saying which.
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
      // `postFormJSON` does not check the status and runs `res.json()`: a
      // non-JSON body (413 from the form ceiling, HTML error page, gunicorn 500)
      // rejected the promise. The `finally` gave the button back and the user
      // saw STRICTLY NOTHING, while nothing had been saved.
      // The pattern is the one of `envoyerForm`, just above.
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
    basculer(enabled);                    // immediate application, without waiting for the network
    if (!(await envoyerForm("/mcp", { action: "toggle", id: String(id), enabled: enabled ? "1" : "0" }))) {
      // The result was ignored: a refusal left the toggle flipped, so the
      // screen asserted a state the server had not saved.
      basculer(!enabled);
    }
  }

  async function deleteMcp(id: number, nom: string) {
    // Permanent deletion (URL + secret) in one click: we confirm.
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
      // Same hole as `saveMcp`: without a `catch`, a silent 413/503 made it
      // look like a save that had not happened.
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
    // …but if the server refuses, we REVERT to the previous theme: keeping an
    // unsaved choice would make it look like it holds, until reload.
    if (!(await envoyerForm("/settings/appearance", { theme_id: id }))) setThemeId(precedent);
  }

  async function selectLang(l: Lang) {
    const precedent = lang;
    setLang(l);
    if (!(await envoyerForm("/settings/appearance", { lang: l }))) setLang(precedent);
  }

  async function selectAvatar(avatarId: string) {
    const precedent = data?.avatar_id ?? null;
    // `""` (« generated » choice) normalizes to `null`: it is how the rest of the
    // application represents « no logo chosen », and thus the generated avatar.
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
  // The playground's skills (the « / » menu): built-in + the user's own. They
  // live in the browser (lib/skills.ts) and were INVISIBLE here — « on voit les
  // skills qu'il y a et même par défaut » : the defaults now have a home in the
  // panel too.
  const [skillsPlayground] = useState<SkillPlayground[]>(() => [...BASE_SKILLS, ...loadCustomSkills()]);

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
                <UserAvatar avatarId={data?.avatar_id} username={acct?.username}
                            name={acct?.fullname} size="sm" />
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
                <AccountSection acct={acct} act={act} avatarId={data?.avatar_id} pct={pct} />
              )}

              {/* ── Usage ──────────────────────────────────────────────── */}
              {section === "usage" && (
                <UsageSection limits={limits} refresh={refresh} />
              )}

              {/* ── Appearance ──────────────────────────────────────────── */}
              {section === "appearance" && (
                <AppearanceSection mode={mode} setMode={setMode} themeId={themeId} selectTheme={selectTheme} lang={lang} selectLang={selectLang} />
              )}

              {/* ── API keys ───────────────────────────────────────────── */}
              {section === "keys" && <KeysContent />}
              {section === "memory" && <MemoryContent />}

              {/* ── Personalization ───────────────────────────────────── */}
              {section === "avatar" && (
                <AvatarSection data={data} acct={acct} selectAvatar={selectAvatar} />
              )}

              <McpSection
                active={section === "mcp"}
                data={data}
                isAddingMcp={isAddingMcp}
                setIsAddingMcp={setIsAddingMcp}
                editingMcpId={editingMcpId}
                setEditingMcpId={setEditingMcpId}
                mcpForm={mcpForm}
                setMcpForm={setMcpForm}
                toggleMcp={toggleMcp}
                deleteMcp={deleteMcp}
              />

              <SkillsSection
                active={section === "skills"}
                data={data}
                skillsPlayground={skillsPlayground}
                isAddingSkill={isAddingSkill}
                setIsAddingSkill={setIsAddingSkill}
                editingSkillId={editingSkillId}
                setEditingSkillId={setEditingSkillId}
                skillForm={skillForm}
                setSkillForm={setSkillForm}
                deleteSkill={deleteSkill}
              />
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
