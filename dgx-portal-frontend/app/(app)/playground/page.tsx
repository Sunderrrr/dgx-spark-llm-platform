import { serverRead, pageSeed } from "@/lib/server-api";
import { conversationsFromApi, type ApiConversations } from "@/lib/conversations";
import type { PlaygroundData } from "@/lib/types";
import PlaygroundPage, { type PlaygroundSeed } from "./PlaygroundPage";

// Server-rendered with the current models and the conversation list: see
// lib/server-api.ts.
export default async function Page() {
  const [seed, convs] = await Promise.all([
    pageSeed<{ data: PlaygroundData }>({ data: "/api/playground/data" }),
    serverRead<ApiConversations>("/api/conversations"),
  ]);
  // Titles only, for the sidebar: the full list can weigh up to 8 MB and is
  // still fetched by the page itself. Opening a conversation before it lands
  // goes through the « messages omitted » path (fetched on demand).
  const conversations: PlaygroundSeed["conversations"] | null =
    convs.status === "ok"
      ? conversationsFromApi(convs.data).map((c) => ({ ...c, messages: [], messagesOmis: true }))
      : null;
  // The greeting's « soir » decided here, on the server's clock (TZ_DISPLAY):
  // the client hydrates with it, then follows its own clock.
  const h = new Date().getHours();
  return (
    <PlaygroundPage
      seed={{ id: seed.id, data: { ...seed.data, conversations } }}
      soirServeur={h >= 18 || h < 5}
    />
  );
}
