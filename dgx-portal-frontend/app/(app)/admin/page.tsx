import { pageSeed } from "@/lib/server-api";
import AdminPage, { type AdminSeed } from "./AdminPage";
import { tabDepuis } from "./tabs";

// Server-rendered with the dashboard, the audit log and the platform snapshot
// — or straight in its « reserved » state for a non-admin (403) — and on the
// tab the URL names: see lib/server-api.ts.
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [seed, params] = await Promise.all([
    pageSeed<AdminSeed>({
      admin: "/api/admin",
      audit: "/admin/audit",
      platform: "/admin/platform",
    }),
    searchParams,
  ]);
  const tab = typeof params.tab === "string" ? params.tab : undefined;
  return <AdminPage seed={seed} initialTab={tabDepuis(tab)} />;
}
