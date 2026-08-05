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
  SunIcon,
  MoonIcon,
  ArrowRightOnRectangleIcon,
  Cog6ToothIcon,
  FilmIcon,
  DocumentMagnifyingGlassIcon,
} from "@heroicons/react/24/outline";
import { useThemeMode } from "../theme-provider";
import { useCsrf } from "@/lib/useCsrf";
import { SettingsDialog } from "./_components/SettingsDialog";
import { useT } from "@/lib/i18n";

type Whoami = { username: string; fullname: string; is_admin: boolean; avatar_id: string | null; maintenance_mode: boolean };

// « Mes clés API » n'est volontairement plus ici : sa configuration vit
// désormais dans le dialogue Réglages (onglet « Clés API »), ouvert par
// l'engrenage du pied de la barre latérale. La route /keys reste servie —
// les boutons de la page d'accueil y renvoient toujours.
const NAV_ITEMS = [
  { href: "/", label: "Accueil", icon: HomeIcon },
  { href: "/playground", label: "Playground", icon: ChatBubbleLeftRightIcon },
  { href: "/video", label: "Vidéo", icon: FilmIcon },
  { href: "/ocr", label: "OCR", icon: DocumentMagnifyingGlassIcon },
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
  const t = useT();
  const csrf = useCsrf();

  // /logout est en POST (protégé CSRF) : un simple lien GET permettait à
  // n'importe quel site tiers de nous déconnecter. On soumet donc un vrai
  // formulaire plutôt qu'un fetch, pour que le navigateur NAVIGUE et suive la
  // redirection finale — y compris vers l'end-session Authentik en SSO, qu'un
  // fetch avalerait silencieusement.
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
    ? [...NAV_ITEMS, { href: "/admin", label: "Admin", icon: ShieldCheckIcon }]
    : NAV_ITEMS;

  useEffect(() => {
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then(setWho)
      .catch(() => {});
  }, []);

  const isDark = mode === "dark" || (mode === "system" && typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  return (
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
                  onClick={() => setIsSettingsOpen(true)}
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
      <SettingsDialog
        isOpen={isSettingsOpen}
        onOpenChange={setIsSettingsOpen}
        onAvatarChange={(avatarId) => setWho((prev) => (prev ? { ...prev, avatar_id: avatarId } : prev))}
      />
    </AppShell>
  );
}
