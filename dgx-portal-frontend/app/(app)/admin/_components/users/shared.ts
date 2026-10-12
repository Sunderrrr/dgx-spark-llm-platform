// Shared types and helpers of the admin users section and its per-dialog
// components. Extracted VERBATIM from app/(app)/admin/_components/UsersSection.tsx.
import type { Activity } from "../../../_components/settings/types";
import type { AccountSession } from "../../../_components/SessionsList";

export type LocalUser = {
  username: string;
  fullname: string | null;
  /** Chosen brand logo; `null` = avatar generated from the username. */
  avatar_id: string | null;
  sources: string[];
  managed: boolean;
  managed_by: "local" | "repertoire";
  id: number | null;
  group_name: string | null;
  enabled: number;
  is_admin: number | null;
  effective_admin: boolean | null;
  role_source: "local" | "sso" | "ldap" | "external" | null;
  last_source: string | null;
  effective_budget: number | null;
  unlimited: boolean;
  spend: number;
  key_count: number;
  last_seen: string | null;
  // Blocking / locking state returned by /api/admin/users. A blocked
  // account is refused at login whatever its source (LDAP/SSO included);
  // locked_minutes is the temporary lock after too many login failures.
  blocked: boolean;
  block_reason: string | null;
  blocked_at: string | null;
  locked_minutes: number;
  /** LDAP/SSO accounts: their state in the directory, null when it could not
   *  be asked (and for local accounts, whose state is `enabled`). */
  directory_status?: DirectoryStatus | null;
  /** LDAP/SSO groups of the account (merged by name), from its last login. */
  directory_groups?: string[];
  /** The instance's owner (created at first start): out of reach of the other admins. */
  owner?: boolean;
};

export type DirectoryStatus = "active" | "disabled" | "absent";

/** How a directory state is shown (badge label + variant). */
export const DIRECTORY_STATUS_META: Record<DirectoryStatus, { label: string; variant: "success" | "error" | "warning" }> = {
  active: { label: "Actif", variant: "success" },
  disabled: { label: "Désactivé dans l'annuaire", variant: "error" },
  absent: { label: "Absent de l'annuaire", variant: "warning" },
};
export type Group = {
  name: string;
  max_budget: number | null;
  is_admin: number;
  /** Where it comes from: "local", "ldap", "sso" — one group whatever brought
   *  it (same names are merged). */
  sources?: string[];
  /** Local accounts in it + directory accounts the directory puts in it. */
  members?: number;
};
export type UsersData = {
  users: LocalUser[];
  groups: Group[];
  default_budget: number;
  /** The quota period, e.g. « 7d »: every cap is per period, not per day. */
  budget_duration?: string;
};

/** « / semaine » for 7d, « / jour » for 1d, « / 30 jours » for 30d… — a msgid
 *  and its value, the caller translates. */
export function quotaPeriod(duration: string | undefined): { msgid: string; n?: number } {
  const m = /^(\d+)\s*d$/.exec((duration ?? "7d").trim());
  const days = m ? Number(m[1]) : NaN;
  if (days === 1) return { msgid: "/ jour" };
  if (days === 7) return { msgid: "/ semaine" };
  if (Number.isFinite(days)) return { msgid: "/ {n} jours", n: days };
  return { msgid: "/ période" };
}

// Response of GET /admin/users/<username>/detail. Any field can be missing
// (LDAP/SSO account without a local row, user without a LiteLLM profile…).
export type AdminUserDetail = {
  ok?: boolean;
  username?: string | null;
  fullname?: string | null;
  sources?: string | null; // "ldap,sso" — comma-separated string, not an array
  last_source?: string | null;
  last_seen?: string | null;
  role?: string | null;
  enabled?: boolean;
  local?: boolean;
  group?: string | null;
  max_budget?: number | null;
  effective_budget?: number | null;
  blocked?: { blocked?: boolean; reason?: string | null; at?: string | null; by?: string | null } | null;
  litellm?: { exists?: boolean; max_budget?: number | null; spend?: number | null; budget_duration?: string | null } | null;
  keys?: { alias?: string | null; created_at?: string | null; spend?: number | null }[] | null;
  memory_facts?: number | null;
  conversations?: number | null;
  sessions?: AccountSession[] | null;
  audit?: { action?: string | null; detail?: string | null; by?: string | null; at?: string | null }[] | null;
  /** Daily token series over 6 months (same as the account's own settings). */
  activity?: Activity | null;
  directory_status?: DirectoryStatus | null;
  /** Jobs per media page: how many, and the last one. */
  media?: Record<"image" | "music" | "video" | "voice" | "ocr", { count: number; last: string | null }> | null;
  /** The account's last model requests. */
  model_requests?: { model_id: string; status: string; created_at: string }[] | null;
};

// Category tags (auth source) → non-semantic color variants.
export const SOURCE_META: Record<string, { label: string; variant: "green" | "orange" | "blue" | "purple" | "neutral" }> = {
  local: { label: "Local", variant: "green" },
  ldap: { label: "LDAP", variant: "blue" },
  sso: { label: "SSO", variant: "purple" },
  external: { label: "Externe", variant: "neutral" },
};

export const BOOL_OPTS = (t: (s: string) => string) => [
  { label: t("Non"), value: "0" },
  { label: t("Oui"), value: "1" },
];

/** State of the « Nouvel utilisateur » form. */
export type NewUserForm = {
  username: string;
  password: string;
  fullname: string;
  group: string;
  max_budget: string;
  is_admin: string;
};

/** State of the « Nouveau groupe » form. */
export type NewGroupForm = { name: string; max_budget: string; is_admin: string };
