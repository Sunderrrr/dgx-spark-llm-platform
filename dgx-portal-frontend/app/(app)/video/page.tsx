import { pageSeed } from "@/lib/server-api";
import VideoPage, { type VideoSeed } from "./VideoPage";

// Server-rendered with its data (history + whether the service runs): see
// lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<VideoSeed>({
    history: "/api/video/history",
    running: "/api/running-models",
  });
  return <VideoPage seed={seed} />;
}
