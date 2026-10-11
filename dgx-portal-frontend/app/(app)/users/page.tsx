import { pageSeed } from "@/lib/server-api";
import UsersPage, { type UsersSeed } from "./UsersPage";

// Server-rendered with the account list — or straight in its « reserved to
// administrators » state for anyone else (403): see lib/server-api.ts.
export default async function Page() {
  const seed = await pageSeed<UsersSeed>({ users: "/api/admin/users" });
  return <UsersPage seed={seed} />;
}
