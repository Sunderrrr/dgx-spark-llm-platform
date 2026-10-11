// The ranking's default view, read by the server page and the client alike
// (a constant exported from a "use client" module is only a reference on the
// server side, so it lives here).
export const RANKING_DEFAULT = { period: "day", metric: "total" } as const;
