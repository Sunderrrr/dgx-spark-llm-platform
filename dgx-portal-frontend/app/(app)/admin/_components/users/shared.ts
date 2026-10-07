// Shared types and helpers of the admin users section and its per-dialog
// components. Extracted VERBATIM from app/(app)/admin/_components/UsersSection.tsx.

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
  role_source: "local" | "sso" | "ldap" | "externe" | null;
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
};
export type Group = { name: string; max_budget: number | null; is_admin: number };
export type UsersData = { users: LocalUser[]; groups: Group[]; default_budget: number };

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
};

// Category tags (auth source) → non-semantic color variants.
export const SOURCE_META: Record<string, { label: string; variant: "green" | "orange" | "blue" | "purple" | "neutral" }> = {
  local: { label: "Local", variant: "green" },
  ldap: { label: "LDAP", variant: "blue" },
  sso: { label: "SSO", variant: "purple" },
  externe: { label: "Externe", variant: "neutral" },
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
