import { pageSeed } from "@/lib/server-api";
import RankingPage, { type RankingSeed } from "./RankingPage";
import { RANKING_DEFAULT } from "./defaults";

// Server-rendered with the default view's ranking: see lib/server-api.ts.
export default async function Page() {
  const q = new URLSearchParams(RANKING_DEFAULT).toString();
  const seed = await pageSeed<RankingSeed>({ ranking: `/api/ranking?${q}` });
  return <RankingPage seed={seed} />;
}
