"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { HStack, VStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import {
  HomeIcon,
  ChatBubbleLeftRightIcon,
  MagnifyingGlassIcon,
  BellIcon,
  PaperAirplaneIcon,
  TrophyIcon,
  LifebuoyIcon,
  ShieldCheckIcon,
  UsersIcon,
  ArrowRightOnRectangleIcon,
  Cog6ToothIcon,
  FilmIcon,
  PhotoIcon,
  DocumentMagnifyingGlassIcon,
  SpeakerWaveIcon,
  MusicalNoteIcon,
} from "@heroicons/react/24/outline";
import { useThemeMode } from "../theme-provider";
import { useCsrf, useCsrfRefresh } from "@/lib/useCsrf";
import { useWhoami } from "@/lib/whoami";
import { UserAvatar } from "@/lib/user-avatar";
import { SettingsDialog } from "./_components/SettingsDialog";
import { OnboardingDialog } from "./_components/OnboardingDialog";
import { useT } from "@/lib/i18n";
import { SettingsDialogContext, type SettingsSection } from "@/lib/settings-dialog";
/* The logo, imported instead of pointed at by URL. `import` puts the file
   through the asset pipeline: the browser gets a
   `/_next/static/media/favicon.<empreinte>.ico` URL, hence a NEW URL as soon as
   the file changes (the import yields a `StaticImageData` object, hence `.src`).
   Served instead from `/favicon.ico`, it kept the same address from one version
   to the next and the browser re-served its copy: measured on 2026-10-01, the
   request did not even go back to the server (no network event visible) and
   the old black logo stayed displayed — a failed entry can stick the same way
   (seen once; 10 out of 10 attempts correct afterwards). The tab favicon,
   for its part, keeps being served from the root. */
import logoCronos from "@/app/favicon.ico";
import { useBranding } from "@/lib/branding";

type NotificationItem = {
  id: number;
  kind: string;
  title: string;
  seen: boolean;
  created_at: string;
};

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

// Notification type coming from the server → displayed label (translated at
// render time); the raw type stays the fallback for any future unknown type.
const NOTIF_KIND_LABEL: Record<string, string> = {
  request: "Demande",
  image: "Image",
  music: "Musique",
};

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { mode, setMode } = useThemeMode();
  const { who, setWho } = useWhoami();
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Pending requests (sidebar badge). Light polling to stay up to date
  // without loading the app — not a critical real-time counter.
  const [pendingCount, setPendingCount] = useState<{ model: number; budget: number }>({ model: 0, budget: 0 });
  // Onboarding: opens when the account has never seen it. The state comes from
  // the server (user_prefs.onboarded column), not the browser — it follows the
  // person from one machine to the next, and never comes back once done.
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | undefined>(undefined);
  const t = useT();
  const marque = useBranding();
  const router = useRouter();
  // Command palette (Ctrl/Cmd+K): navigation and quick actions.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteValue, setPaletteValue] = useState("");
  // Notification center (bell): list + unread count.
  const [notifOpen, setNotifOpen] = useState(false);
  const [notifs, setNotifs] = useState<NotificationItem[]>([]);
  const [notifUnread, setNotifUnread] = useState(0);
  const csrf = useCsrf();
  const refreshCsrf = useCsrfRefresh();

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
  //
  // The token is re-requested right before: it is the only POST whose failure is
  // a dead end (the user sees an English « Bad Request » page and can no longer
  // log out). The provider fetches it on document mount, and nothing guarantees
  // it has not changed since — seen after an SSO login. One extra request on the
  // way out beats an impossible logout.
  async function logout() {
    let jeton = csrf;
    try {
      jeton = await refreshCsrf();
    } catch {
      // Flaky network: we post the known token rather than blocking the user.
    }
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/logout";
    const field = document.createElement("input");
    field.type = "hidden";
    field.name = "csrf_token";
    field.value = jeton;
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

  // Command palette: navigation (the same pages as the sidebar) +
  // a few quick actions. Grouped via auxiliaryData.group.
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

  // Keyboard opening (Cmd/Ctrl+K): global listener, outside text fields.
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
    // The onboarding opens when the account has never seen it (server state).
    // Sync "from an external system" (the server's onboarded flag), same rule
    // as the localStorage reconcile in the theme provider.
    /* eslint-disable react-hooks/set-state-in-effect */
    if (who && !who.onboarded) setShowOnboarding(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [who]);

  // « demandes en attente » badge in the sidebar: light counter, updated
  // every 30 s.
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      fetch("/api/pending-count", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : { model: 0, budget: 0 }))
        .then((d) => { if (!cancelled) setPendingCount({ model: d?.model ?? 0, budget: d?.budget ?? 0 }); })
        .catch(() => {});
    tick();
    const id = setInterval(tick, 30000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // The CSRF token is read in a REF, not captured: GET /api/notifications does
  // not need it, but POST /seen does. Putting it in the useCallback
  // dependencies, `loadNotifs` was recreated when `csrf` went from "" to the
  // token, which re-ran the mount effect: the list was loaded TWICE at every
  // page opening, for nothing.
  const csrfRef = useRef(csrf);
  useEffect(() => { csrfRef.current = csrf; }, [csrf]);
  const loadNotifs = useCallback((markSeen: boolean) => {
    fetch("/api/notifications", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { items: [], unread: 0 }))
      .then((d) => {
        setNotifs(d?.items ?? []);
        setNotifUnread(d?.unread ?? 0);
        if (markSeen && (d?.unread ?? 0) > 0) {
          setNotifUnread(0);
          fetch("/api/notifications/seen", { method: "POST", credentials: "include", headers: { "X-CSRFToken": csrfRef.current } }).catch(() => {});
        }
      })
      .catch(() => {});
  }, []);
  useEffect(() => { loadNotifs(false); }, [loadNotifs]);

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
              heading={marque.name}
              /* The tab logo (favicon served from the root) also at the
                 top left, right before « Cronos ». empty alt: the info is
                 already carried by the text of the heading that follows. */
              icon={
                // Static 26 KB favicon: next/image brings nothing here.
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={marque.logo ?? logoCronos.src}
                  alt=""
                  style={{ width: "var(--spacing-5)", height: "var(--spacing-5)" }}
                />
              }
              headingHref="/"
            />
          }
          footer={
            <HStack padding={2} gap={2} vAlign="center" hAlign="between" wrap="wrap">
              <HStack gap={2} vAlign="center" wrap="wrap">
                {/* Always rendered: without a chosen logo, it is the avatar generated from the username. */}
                <UserAvatar
                  avatarId={who?.avatar_id}
                  username={who?.username}
                  name={who?.fullname}
                  size="sm"
                />
                <Text type="supporting" color="secondary" maxLines={1}>
                  {who?.fullname || ""}
                </Text>
              </HStack>
              <HStack gap={1} vAlign="center" hAlign="end" wrap="wrap">
                <Button
                  label={t("Recherche rapide")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={MagnifyingGlassIcon} size="sm" />}
                  onClick={() => setPaletteOpen(true)}
                />
                <Button
                  label={t("Notifications")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={BellIcon} size="sm" />}
                  onClick={() => { setNotifOpen(true); loadNotifs(true); }}
                />
                {notifUnread > 0 && <Badge label={String(notifUnread)} variant="info" />}
                <Button
                  label={t("Réglages")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                  onClick={() => openSettings()}
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
                  /* SEPARATE badges: « Demander un modèle » only counts model
                     requests; « Admin » carries the processing queue
                     (models + budgets). A mixed total displayed « 1 » on
                     the model request for a budget request. */
                  item.href === "/request" && pendingCount.model > 0
                    ? <Badge label={String(pendingCount.model)} variant="info" />
                    : item.href === "/admin" && pendingCount.model + pendingCount.budget > 0
                      ? <Badge label={String(pendingCount.model + pendingCount.budget)} variant="info" />
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
      {/* The overlays always mounted (onboarding, settings, palette,
          notifications) come BEFORE {children}. Measured reason: the page suspends
          on server rendering (`loading.tsx`), so everything after it in this layout
          is sent in the stream BEFORE it and ends up inserted before it in the DOM,
          where React expects the JSX order (the onboarding-tour text came out at
          byte 34 116 and the page's at 67 239). Here, both orders coincide.
          Note: this does NOT fix the React hydration error #418 observed on
          /playground, whose cause is elsewhere — it persists at an unchanged rate
          after this move (measured). An open <dialog> lives in the « top layer »:
          its position in the DOM changes nothing about its stacking. */}
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
      <Dialog isOpen={notifOpen} onOpenChange={(o) => { if (!o) setNotifOpen(false); }} width={480}>
        <DialogHeader
          title={t("Notifications")}
          subtitle={notifUnread > 0 ? String(notifUnread) : undefined}
          hasDivider
          onOpenChange={(o) => { if (!o) setNotifOpen(false); }}
        />
        <VStack gap={1} padding={3} height={420} isScrollable>
          {notifs.length === 0 ? (
            <Text color="secondary">{t("Aucune notification")}</Text>
          ) : (
            notifs.map((n) => (
              <HStack key={n.id} gap={2} vAlign="center">
                {!n.seen && <StatusDot variant="accent" label={t(NOTIF_KIND_LABEL[n.kind] ?? n.kind)} />}
                <VStack gap={0}>
                  <Text weight={n.seen ? undefined : "semibold"}>{n.title}</Text>
                  <Text type="supporting" color="secondary">
                    <Timestamp value={n.created_at} format="date_time" />
                  </Text>
                </VStack>
              </HStack>
            ))
          )}
        </VStack>
      </Dialog>
      {children}
    </AppShell>
    </SettingsDialogContext.Provider>
  );
}
