"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { AppShell } from "@astryxdesign/core/AppShell";
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
import {
  SparklesIcon,
  HomeIcon,
  KeyIcon,
  ChatBubbleLeftRightIcon,
  MagnifyingGlassIcon,
  PaperAirplaneIcon,
  TrophyIcon,
  LifebuoyIcon,
  ShieldCheckIcon,
  SunIcon,
  MoonIcon,
  ArrowRightOnRectangleIcon,
} from "@heroicons/react/24/outline";
import { useThemeMode } from "../theme-provider";

type Whoami = { username: string; fullname: string; is_admin: boolean };

const NAV_ITEMS = [
  { href: "/", label: "Accueil", icon: HomeIcon },
  { href: "/keys", label: "Mes clés API", icon: KeyIcon },
  { href: "/playground", label: "Playground", icon: ChatBubbleLeftRightIcon },
  { href: "/search", label: "Chercher un modèle", icon: MagnifyingGlassIcon },
  { href: "/request", label: "Demander un modèle", icon: PaperAirplaneIcon },
  { href: "/ranking", label: "Classement", icon: TrophyIcon },
  { href: "/support", label: "Support", icon: LifebuoyIcon },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { mode, setMode } = useThemeMode();
  const [who, setWho] = useState<Whoami | null>(null);

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
              <Text type="supporting" color="secondary" maxLines={1}>
                {who?.fullname || ""}
              </Text>
              <HStack gap={1}>
                <Button
                  label="Basculer le thème"
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={isDark ? SunIcon : MoonIcon} size="sm" />}
                  onClick={() => setMode(isDark ? "light" : "dark")}
                />
                <Button
                  label="Déconnexion"
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={ArrowRightOnRectangleIcon} size="sm" />}
                  onClick={() => {
                    window.location.href = "/logout";
                  }}
                />
              </HStack>
            </HStack>
          }>
          <SideNavSection title="Menu" isHeaderHidden>
            {navItems.map((item) => (
              <SideNavItem
                key={item.href}
                label={item.label}
                icon={item.icon}
                href={item.href}
                isSelected={item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)}
              />
            ))}
          </SideNavSection>
        </SideNav>
      }>
      {children}
    </AppShell>
  );
}
