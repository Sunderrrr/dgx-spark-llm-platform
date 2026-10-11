import { pageSeed } from "@/lib/server-api";
import SupportPage, { type SupportSeed } from "./SupportPage";

// Server-rendered with its data (current model, previous thread, whether the
// account has an API key): see lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<SupportSeed>({
    data: "/api/playground/data",
    thread: "/api/support/thread",
    keys: "/api/keys",
  });
  // The page only needs to know whether a key EXISTS: the key values must not
  // be written into the HTML.
  const keys = seed.data.keys;
  if (keys) seed.data.keys = { user_keys: (keys.user_keys ?? []).map(() => null) };
  return <SupportPage seed={seed} />;
}
