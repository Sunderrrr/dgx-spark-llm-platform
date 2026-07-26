import type { Conversation } from "./types";

const STORAGE_KEY = "pg_convs";
const MAX_CONVERSATIONS = 10;

export function loadConversations(): Conversation[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    return [];
  }
}

export function saveConversations(convs: Conversation[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(convs.slice(0, MAX_CONVERSATIONS)),
    );
  } catch {
    // quota exceeded or storage unavailable — historique non persistant, sans casser l'UI
  }
}

export function relativeTime(ts: number): string {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return "à l'instant";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} j`;
}
