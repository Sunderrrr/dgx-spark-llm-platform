"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { AppShell } from "@astryxdesign/core/AppShell";
import { Banner } from "@astryxdesign/core/Banner";
import { CommandPalette } from "@astryxdesign/core/CommandPalette";
import { createStaticSource } from "@astryxdesign/core/Typeahead";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
} from "@astryxdesign/core/SideNav";
import { NavIcon } from "@astryxdesign/core/NavIcon";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Avatar } from "@astryxdesign/core/Avatar";
import {
  SparklesIcon,
  HomeIcon,
  ChatBubbleLeftRightIcon,
  MagnifyingGlassIcon,
  PaperAirplaneIcon,
  TrophyIcon,
  LifebuoyIcon,
  ShieldCheckIcon,
  UsersIcon,
  SunIcon,
  MoonIcon,
  ArrowRightOnRectangleIcon,
  Cog6ToothIcon,
  FilmIcon,
  PhotoIcon,
  DocumentMagnifyingGlassIcon,
  SpeakerWaveIcon,
  MusicalNoteIcon,
} from "@heroicons/react/24/outline";
import { useThemeMode } from "../theme-provider";
import { useCsrf } from "@/lib/useCsrf";
import { useWhoami } from "@/lib/whoami";
import { SettingsDialog } from "./_components/SettingsDialog";
import { OnboardingDialog } from "./_components/OnboardingDialog";
import { useT } from "@/lib/i18n";
import { SettingsDialogContext, type SettingsSection } from "@/lib/settings-dialog";

// "My API keys" is deliberately no longer here: its configuration now lives
// in the Settings dialog ("API keys" tab), opened by the gear at the bottom
// of the sidebar. There is no more /keys page: the home page's "API keys"
// buttons open this dialog on the "keys" tab via the SettingsDialogContext
// (see openSettings below).
const NAV_ITEMS = [
  { href: "/", label: "Accueil", icon: HomeIcon },
  { href: "/playground", label: "Playground", icon: ChatBubbleLeftRightIcon },
  { href: "/video", label: "Vidéo", icon: FilmIcon },
  { href: "/image", label: "Image", icon: PhotoIcon },
  { href: "/ocr", label: "OCR", icon: DocumentMagnifyingGlassIcon },
  { href: "/voice", label: "Voix", icon: SpeakerWaveIcon },
  { href: "/music", label: "Musique", icon: MusicalNoteIcon },
  { href: "/search", label: "Chercher un modèle", icon: MagnifyingGlassIcon },
  { href: "/request", label: "Demander un modèle", icon: PaperAirplaneIcon },
  { href: "/ranking", label: "Classement", icon: TrophyIcon },
  { href: "/support", label: "Support", icon: LifebuoyIcon },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { mode, setMode } = useThemeMode();
  const { who, setWho } = useWhoami();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Demandes en attente (badge de la sidebar). Poll léger pour rester à jour
  // sans charger l'app — pas un compteur temps réel critique.
  const [pendingCount, setPendingCount] = useState(0);
  // Prise en main : ouverte quand le compte ne l'a jamais vue. L'état vient du
  // serveur (colonne user_prefs.onboarded), pas du navigateur — elle suit donc
  // la personne d'un poste à l'autre, et ne revient jamais une fois passée.
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | undefined>(undefined);
  const t = useT();
  const router = useRouter();
  // Palette de commandes (Ctrl/Cmd+K) : navigation et actions rapides.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteValue, setPaletteValue] = useState("");
  const csrf = useCsrf();

  // Opens the Settings dialog, optionally on a specific tab. Used by the gear
  // (no argument) and by the home page's "API keys" buttons.
  const openSettings = (section?: SettingsSection) => {
    setSettingsSection(section);
    setIsSettingsOpen(true);
  };

  // /logout is POST (CSRF-protected): a plain GET link let any third-party
  // site log us out. So we submit a real form rather than a fetch, so the
  // browser NAVIGATES and follows the final redirect — including to
  // Authentik's end-session under SSO, which a fetch would silently swallow.
  function logout() {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/logout";
    const field = document.createElement("input");
    field.type = "hidden";
    field.name = "csrf_token";
    field.value = csrf;
    form.appendChild(field);
    document.body.appendChild(form);
    form.submit();
  }

  const navItems = useMemo(
    () => (who?.is_admin
      ? [...NAV_ITEMS,
         { href: "/users", label: "Utilisateurs", icon: UsersIcon },
         { href: "/admin", label: "Admin", icon: ShieldCheckIcon }]
      : NAV_ITEMS),
    [who?.is_admin],
  );

  // Palette de commandes : navigation (les mêmes pages que la sidebar) +
  // quelques actions rapides. Groupe via auxiliaryData.group.
  const paletteItems = useMemo(
    () => [
      ...navItems.map((it) => ({
        id: it.href,
        label: t(it.label),
        auxiliaryData: { group: t("Navigation") },
      })),
      { id: "theme", label: t("Basculer le thème"), auxiliaryData: { group: t("Actions") } },
      { id: "logout", label: t("Déconnexion"), auxiliaryData: { group: t("Actions") } },
    ],
    [navItems, t],
  );
  const paletteSource = useMemo(
    () => createStaticSource(paletteItems, { keywords: (i) => [i.auxiliaryData?.group ?? ""] }),
    [paletteItems],
  );

  // Ouverture au clavier (Cmd/Ctrl+K) : listener global, hors champs de saisie.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function onPaletteSelect(id: string) {
    setPaletteValue("");
    setPaletteOpen(false);
    if (id === "theme") setMode(isDark ? "light" : "dark");
    else if (id === "logout") logout();
    else router.push(id);
  }

  useEffect(() => {
    // La prise en main s'ouvre quand le compte ne l'a jamais vue (état serveur).
    // Sync "from an external system" (the server's onboarded flag), same rule
    // as the localStorage reconcile in the theme provider.
    /* eslint-disable react-hooks/set-state-in-effect */
    if (who && !who.onboarded) setShowOnboarding(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [who]);

  // Badge « demandes en attente » dans la sidebar : compteur léger, mis à jour
  // toutes les 30 s.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      fetch("/api/pending-count", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : { count: 0 }))
        .then((d) => { if (!cancelled) setPendingCount(d?.count ?? 0); })
        .catch(() => {});
    tick();
    const id = setInterval(tick, 30000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const isDark = mode === "dark" || (mode === "system" && typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  return (
    <SettingsDialogContext.Provider value={{ open: openSettings }}>
    <AppShell
      contentPadding={0}
      sideNav={
        <SideNav
          resizable={{ defaultWidth: 260, minWidth: 220, maxWidth: 360 }}
          header={
            <SideNavHeading
              heading="Cronos"
              icon={<NavIcon icon={<Icon icon={SparklesIcon} size="sm" />} />}
              headingHref="/"
            />
          }
          footer={
            <HStack padding={2} gap={2} vAlign="center" hAlign="between">
              <HStack gap={2} vAlign="center">
                {who?.avatar_id && <Avatar src={`/avatars/${who.avatar_id}.svg`} name={who.fullname} size="sm" />}
                <Text type="supporting" color="secondary" maxLines={1}>
                  {who?.fullname || ""}
                </Text>
              </HStack>
              <HStack gap={1}>
                <Button
                  label={t("Recherche rapide")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={MagnifyingGlassIcon} size="sm" />}
                  onClick={() => setPaletteOpen(true)}
                />
                <Button
                  label={t("Réglages")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                  onClick={() => openSettings()}
                />
                <Button
                  label={t("Basculer le thème")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={isDark ? SunIcon : MoonIcon} size="sm" />}
                  onClick={() => setMode(isDark ? "light" : "dark")}
                />
                <Button
                  label={t("Déconnexion")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={ArrowRightOnRectangleIcon} size="sm" />}
                  onClick={logout}
                />
              </HStack>
            </HStack>
          }>
          <SideNavSection title="Menu" isHeaderHidden>
            {navItems.map((item) => (
              <SideNavItem
                key={item.href}
                label={t(item.label)}
                icon={item.icon}
                href={item.href}
                isSelected={item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)}
                endContent={
                  pendingCount > 0 && (item.href === "/request" || item.href === "/admin")
                    ? <Badge label={String(pendingCount)} variant="info" />
                    : undefined
                }
              />
            ))}
          </SideNavSection>
        </SideNav>
      }>
      {who?.maintenance_mode && !who.is_admin && (
        <Banner
          status="warning"
          container="section"
          title={t("Mode maintenance en cours")}
          description={t(
            "L'accès à l'API et aux fonctionnalités du site est temporairement suspendu. Réessaie plus tard.",
          )}
        />
      )}
      {children}
      <OnboardingDialog
        isOpen={showOnboarding}
        prenom={who?.fullname?.split(" ")[0]}
        onClose={() => {
          setShowOnboarding(false);
          void fetch("/api/onboarding/done", {
            method: "POST",
            credentials: "include",
            headers: { "X-CSRFToken": csrf },
          }).catch(() => {});
        }}
      />
      <SettingsDialog
        isOpen={isSettingsOpen}
        onOpenChange={setIsSettingsOpen}
        initialSection={settingsSection}
        onAvatarChange={(avatarId) => setWho((prev) => (prev ? { ...prev, avatar_id: avatarId } : prev))}
      />
      <CommandPalette
        isOpen={paletteOpen}
        onOpenChange={setPaletteOpen}
        searchSource={paletteSource}
        value={paletteValue}
        onValueChange={onPaletteSelect}
        label={t("Recherche rapide")}
        emptyBootstrapText={t("Commence à taper pour chercher")}
        emptySearchText={t("Aucun résultat")}
      />
    </AppShell>
    </SettingsDialogContext.Provider>
  );
}
