import { serverRead, pageSeed } from "@/lib/server-api";
import { conversationsFromApi, type ApiConversations } from "@/lib/conversations";
import HomePage, { type HomeSeed } from "./HomePage";

// Server-rendered with the dashboard, the API latency and the conversations to
// resume: see lib/server-api.ts.
export default async function Page() {
  const [seed, convs] = await Promise.all([
    pageSeed<Omit<HomeSeed, "recent">>({ home: "/api/home", latency: "/api/latency" }),
    serverRead<ApiConversations>("/api/conversations"),
  ]);
  // Only the three cards' metadata: the full list can weigh megabytes and
  // would be written into the page for nothing.
  const recent =
    convs.status === "ok"
      ? conversationsFromApi(convs.data).slice(0, 3).map((c) => ({ ...c, messages: [], omittedMessages: true }))
      : null;
  return <HomePage seed={{ id: seed.id, data: { ...seed.data, recent } }} />;
}
