"use client";

// Liste des sessions d'un compte, partagée par la sécurité self-service
// (Réglages → Mon compte) et le détail d'un utilisateur côté admin — même
// présentation partout : indice navigateur/OS lisible, IP et dates.
// L'identifiant de session n'est JAMAIS affiché : opaque, il ne sert qu'à
// la révocation côté serveur.
import type { ReactNode } from "react";
import { List, ListItem } from "@astryxdesign/core/List";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { ComputerDesktopIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";

export type AccountSession = {
  id: string;
  created_at: number;
  expires_at?: number | null;
  ip?: string | null;
  user_agent?: string | null;
  current?: boolean;
};

// Indice navigateur/OS le plus courant. L'ordre compte : Edge/Opera/Android
// contiennent aussi les marqueurs Chrome/Safari/Linux, il faut les tester
// avant.
const BROWSERS: [RegExp, string][] = [
  [/Edg\//, "Edge"],
  [/OPR\//, "Opera"],
  [/SamsungBrowser\//, "Samsung Internet"],
  [/Chrome\//, "Chrome"],
  [/Firefox\//, "Firefox"],
  [/curl\//, "curl"],
  [/Wget\//, "Wget"],
  [/python-requests/, "Python requests"],
  [/Safari\//, "Safari"],
];
const OSES: [RegExp, string][] = [
  [/Windows NT/, "Windows"],
  [/Android/, "Android"],
  [/iPhone|iPad/, "iOS"],
  [/CrOS/, "ChromeOS"],
  [/Mac OS X|Macintosh/, "macOS"],
  [/Linux/, "Linux"],
];

/** Chaîne user-agent → indice lisible (« Chrome · Windows »). Client inconnu :
 * tronqué, la chaîne complète reste sous le title du Text tronqué. */
export function describeUserAgent(ua?: string | null): string {
  const raw = (ua ?? "").trim();
  if (!raw) return "—";
  const browser = BROWSERS.find(([re]) => re.test(raw))?.[1];
  const os = OSES.find(([re]) => re.test(raw))?.[1];
  const hint = [browser, os].filter(Boolean).join(" · ");
  if (hint) return hint;
  return raw.length > 48 ? `${raw.slice(0, 48)}…` : raw;
}

export function SessionsList({
  sessions,
  rowAction,
}: {
  sessions: AccountSession[];
  /** Action par ligne (ex. bouton Révoquer côté self-service). Les sessions
   * courantes (« Cet appareil ») n'ont jamais d'action. */
  rowAction?: (s: AccountSession) => ReactNode;
}) {
  const t = useT();
  if (!sessions.length) {
    return (
      <EmptyState
        icon={<Icon icon={ComputerDesktopIcon} size="lg" />}
        title={t("Aucune session active")}
        description={t("Les sessions apparaîtront ici.")}
        isCompact
      />
    );
  }
  return (
    <List hasDividers>
      {sessions.map((s) => (
        <ListItem
          key={s.id}
          startContent={<Icon icon={ComputerDesktopIcon} size="sm" color="secondary" />}
          label={s.user_agent ? describeUserAgent(s.user_agent) : t("Appareil inconnu")}
          description={
            <VStack gap={0}>
              {/* Chaîne complète tronquée sur une ligne : le Text tronqué
                  expose l'intégralité au survol (title). */}
              {s.user_agent ? (
                <Text type="supporting" color="secondary" maxLines={1}>
                  {s.user_agent}
                </Text>
              ) : null}
              {/* Session ouverte avant que le portail ne note l'IP et le
                  navigateur : un tiret seul laissait croire à un appareil
                  exotique au lieu d'un trou dans les données. Le porteur
                  complète sa propre ligne en ouvrant cette liste. */}
              {!s.user_agent && !s.ip ? (
                <Text type="supporting" color="secondary">
                  {t("Origine non enregistrée : session ouverte avant que le portail ne note l'IP et le navigateur.")}
                </Text>
              ) : null}
              <HStack gap={1} vAlign="center">
                {s.ip ? (
                  <Text type="supporting" color="secondary" hasTabularNumbers>
                    {s.ip}
                  </Text>
                ) : null}
                {s.created_at ? (
                  <HStack gap={1} vAlign="center">
                    {s.ip ? (
                      <Text type="supporting" color="secondary">
                        ·
                      </Text>
                    ) : null}
                    <Timestamp value={s.created_at} format="date_time" />
                  </HStack>
                ) : null}
                {s.expires_at ? (
                  <HStack gap={1} vAlign="center">
                    <Text type="supporting" color="secondary">
                      · {t("Expire")}
                    </Text>
                    <Timestamp value={s.expires_at} format="date_time" />
                  </HStack>
                ) : null}
              </HStack>
            </VStack>
          }
          endContent={
            s.current ? <Badge label={t("Cet appareil")} variant="success" /> : rowAction?.(s)
          }
        />
      ))}
    </List>
  );
}
