import { pageSeed } from "@/lib/server-api";
import DecisionsPage, { type DecisionsSeed } from "./DecisionsPage";

// Server-rendered with the service's state: the page has something true to show
// before any JavaScript runs, and the client refreshes it on demand (a decision
// engine starts and stops outside the page).
export default async function Page() {
  const seed = await pageSeed<DecisionsSeed>({ status: "/api/decisions" });
  return <DecisionsPage seed={seed} />;
}
