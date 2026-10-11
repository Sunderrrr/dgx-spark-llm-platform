import { pageSeed } from "@/lib/server-api";
import OcrPage, { type OcrSeed } from "./OcrPage";

// Server-rendered with its data (history + whether the service runs): see
// lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<OcrSeed>({
    history: "/api/ocr/history",
    running: "/api/running-models",
  });
  return <OcrPage seed={seed} />;
}
