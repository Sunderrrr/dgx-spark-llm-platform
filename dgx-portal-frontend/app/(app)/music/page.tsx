import { pageSeed } from "@/lib/server-api";
import MusicPage, { type MusicSeed } from "./MusicPage";

// Server-rendered with its data (history + whether the service runs): see
// lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<MusicSeed>({
    history: "/api/music/history",
    running: "/api/running-models",
  });
  return <MusicPage seed={seed} />;
}
