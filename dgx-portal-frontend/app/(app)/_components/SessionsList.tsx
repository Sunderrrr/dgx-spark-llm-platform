"use client";

// List of an account's sessions, shared by the self-service security
// (Réglages → Mon compte) and the admin-side user detail — same
// presentation everywhere: readable browser/OS hint, IP and dates.
// The session identifier is NEVER displayed: opaque, it is only used for
// server-side revocation.
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

// Most common browser/OS hint. Order matters: Edge/Opera/Android
// also contain the Chrome/Safari/Linux markers, so they must be tested
// first.
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

/** User-agent string → readable hint (« Chrome · Windows »). Unknown client:
 * truncated, the full string stays under the title of the truncated Text. */
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
  /** Action per row (e.g. the self-service Révoquer button). Current
   * sessions (« Cet appareil ») never have an action. */
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
              {/* Full string truncated on one line: the truncated Text
                  exposes the whole of it on hover (title). */}
              {s.user_agent ? (
                <Text type="supporting" color="secondary" maxLines={1}>
                  {s.user_agent}
                </Text>
              ) : null}
              {/* Session opened before the portal recorded the IP and the
                  browser: a bare dash looked like an exotic device instead
                  of a gap in the data. The bearer completes their own row
                  by opening this list. */}
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
