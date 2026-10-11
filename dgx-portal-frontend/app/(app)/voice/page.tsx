import { pageSeed } from "@/lib/server-api";
import VoicePage, { type VoiceSeed } from "./VoicePage";

// Server-rendered with its data (history, whether the voice service runs, and
// its languages): see lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<VoiceSeed>({
    history: "/api/voice/history",
    running: "/api/running-models",
    info: "/api/voice/info",
  });
  return <VoicePage seed={seed} />;
}
