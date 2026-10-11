import { pageSeed } from "@/lib/server-api";
import ImagePage, { type ImageSeed } from "./ImagePage";

// Server-rendered with its data (history + whether the image service runs):
// see lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<ImageSeed>({
    history: "/api/image/history",
    running: "/api/running-models",
  });
  return <ImagePage seed={seed} />;
}
