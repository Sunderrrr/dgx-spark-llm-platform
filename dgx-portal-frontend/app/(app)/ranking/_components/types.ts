// Shapes of the leaderboard API (dgx-portal/ranking.py, ranking_routes.py)
// and the few formatters the page's parts share.

export interface RankRow extends Record<string, unknown> {
  rank: number | null;
  /** null when the account chose to appear anonymously (not for itself, nor an admin). */
  username: string | null;
  anon_id?: string;
  /** The chosen brand logo, or null for the avatar generated from the username. */
  avatar_id?: string | null;
  is_me: boolean;
  is_group: boolean;
  members: number | null;
  is_anonymous?: boolean;
  is_unattributed: boolean;
  value: number;
  tokens: number;
  prompt: number;
  completion: number;
  requests: number;
  share_pct: number | null;
  delta: number | null;
  trend: number[];
  models: { model: string; share_pct: number }[];
  cost: number;
  /** Admin only: share of the LiteLLM budget used (null = unlimited). */
  quota_pct?: number | null;
}

export type Prices = { input: number; output: number; currency: string };

export type RankingData = {
  rows: RankRow[];
  active_count: number;
  period: string;
  metric: string;
  by: "account" | "group";
  model: string | null;
  models: { model: string; requests: number; tokens: number }[];
  total: number;
  avg: number;
  cost_total: number;
  prices: Prices;
  has_prev: boolean;
  period_label: string;
  prev_label: string;
  is_admin: boolean;
  me_anonymous: boolean;
};

export type Activity = {
  period: string;
  cells: { requests: number; tokens: number }[][];
  max_requests: number;
  total_requests: number;
};

type Named = { username: string | null; is_me: boolean };
export type Records = {
  best_days: (Named & { day: string; tokens: number; requests: number })[];
  longest_streaks: (Named & { days: number })[];
  current_streaks: (Named & { days: number })[];
  firsts: (Named & { model: string; day: string })[];
  me: null | {
    best_day: { day: string; tokens: number };
    longest_streak: number;
    current_streak: number;
    active_days: number;
    first_on: string[];
  };
};

export type UserDetail = {
  username: string;
  period: string;
  models: { model: string; prompt: number; completion: number; requests: number }[];
  daily: { day: string; tokens: number; requests: number }[];
  prompt: number;
  completion: number;
  requests: number;
  cost: number;
  currency?: string;
};

/** Percentage in the displayed language: « 30,89 % » in French, « 30.89% » in English. */
export function pct(v: number, locale: string, decimals = 2) {
  return (v / 100).toLocaleString(locale, {
    style: "percent", minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  });
}

export function num(v: number, locale: string) {
  return Math.round(v).toLocaleString(locale);
}

/** A reference cost: « 12,40 $US », « $12.40 ». */
export function money(v: number, currency: string, locale: string) {
  try {
    return v.toLocaleString(locale, { style: "currency", currency, maximumFractionDigits: v < 10 ? 2 : 0 });
  } catch {
    return `${v.toLocaleString(locale)} ${currency}`;
  }
}
