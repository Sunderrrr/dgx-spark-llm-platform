// Shared types of the settings dialog and its per-section components.
// Extracted VERBATIM from app/(app)/_components/SettingsDialog.tsx.

import type { ActivityDay } from "../ActivityHeatmap";

export type McpServer = {
  id: number;
  name: string;
  url: string;
  description: string;
  allowed_tools: string;
  enabled: number;
  has_auth: number;
  created_at: string;
};
export type Skill = { id: number; name: string; description: string; instructions: string; created_at: string };
export type AvatarChoice = { id: string; label: string };
export type Account = {
  username: string;
  fullname: string;
  is_admin: boolean;
  spend: number;
  max_budget: number | null;
  // End of the LiteLLM envelope period: that is where `spend` restarts from zero.
  budget_reset_at: string | null;
  budget_duration: string;
  unlimited: boolean;
  key_count: number;
  mcp_count: number;
  skill_count: number;
};
export type Activity = {
  days: ActivityDay[];
  total: number;
  prompt: number;
  completion: number;
  peak: number;
  peak_day: string | null;
  active_days: number;
  avg: number;
};
export type Limit = {
  key: string;
  label: string;
  desc: string;
  used: number | null;
  max: number | null;
  unit: string;
  unlimited: boolean;
};
export type SettingsData = {
  activity: Activity;
  limits: Limit[];
  mcp_servers: McpServer[];
  skills: Skill[];
  avatar_id: string | null;
  avatars: AvatarChoice[];
  account: Account;
};

export type Section = "account" | "usage" | "keys" | "memory" | "avatar" | "appearance" | "mcp" | "skills";

/** State of the MCP create/edit form. */
export type McpForm = { name: string; url: string; description: string; allowedTools: string; auth: string };

/** State of the skill create/edit form. */
export type SkillForm = { name: string; description: string; instructions: string };

export const EMPTY_ACTIVITY: Activity = {
  days: [], total: 0, prompt: 0, completion: 0,
  peak: 0, peak_day: null, active_days: 0, avg: 0,
};
