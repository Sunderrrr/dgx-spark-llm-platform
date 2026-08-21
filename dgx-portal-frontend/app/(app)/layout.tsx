"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { AppShell } from "@astryxdesign/core/AppShell";
import { Banner } from "@astryxdesign/core/Banner";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
} from "@astryxdesign/core/SideNav";
import { NavIcon } from "@astryxdesign/core/NavIcon";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
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
import { SettingsDialog } from "./_components/SettingsDialog";
import { OnboardingDialog } from "./_components/OnboardingDialog";
import { useT } from "@/lib/i18n";
import { SettingsDialogContext, type SettingsSection } from "@/lib/settings-dialog";

type Whoami = { username: string; fullname: string; is_admin: boolean; avatar_id: string | null; maintenance_mode: boolean; onboarded: boolean };

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
  const [who, setWho] = useState<Whoami | null>(null);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Prise en main : ouverte quand le compte ne l'a jamais vue. L'état vient du
  // serveur (colonne user_prefs.onboarded), pas du navigateur — elle suit donc
  // la personne d'un poste à l'autre, et ne revient jamais une fois passée.
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | undefined>(undefined);
  const t = useT();
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

  const navItems = who?.is_admin
    ? [...NAV_ITEMS,
       { href: "/users", label: "Utilisateurs", icon: UsersIcon },
       { href: "/admin", label: "Admin", icon: ShieldCheckIcon }]
    : NAV_ITEMS;

  useEffect(() => {
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: Whoami | null) => {
        setWho(d);
        if (d && !d.onboarded) setShowOnboarding(true);
      })
      .catch(() => {});
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
    </AppShell>
    </SettingsDialogContext.Provider>
  );
}
