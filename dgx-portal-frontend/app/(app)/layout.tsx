import { pageSeed } from "@/lib/server-api";
import AppLayout, { type ShellSeed } from "./AppLayout";

// The shell is server-rendered with its badges (pending requests,
// notifications), read alongside the page's own data: see lib/server-api.ts.
export default async function Layout({ children }: { children: React.ReactNode }) {
  const seed = await pageSeed<ShellSeed>({
    pending: "/api/pending-count",
    notifs: "/api/notifications",
  });
  return <AppLayout seed={seed}>{children}</AppLayout>;
}
