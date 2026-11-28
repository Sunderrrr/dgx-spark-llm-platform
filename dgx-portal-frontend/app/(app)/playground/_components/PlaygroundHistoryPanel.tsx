"use client";

// The playground's conversation history: the searched, pinned-first list and
// the HistoryDialog that renders it. Extracted from PlaygroundPage.tsx (plain
// props, no behaviour change) — the page keeps the conversations state and
// every mutation (rename, pin, delete, share, export).
//
// The list is computed HERE, next to its only reader, and only while the dialog
// is open: the filter/sort used to run on every render, hence on every token
// received, while the list is only read in the « Historique » box.

import { useMemo } from "react";
import type { Conversation } from "@/lib/types";
import type { ExportConversation } from "@/lib/export";
import { HistoryDialog } from "./HistoryDialog";

export type PlaygroundHistoryPanelProps = {
  historyOpen: boolean;
  setHistoryOpen: (v: boolean) => void;
  conversations: Conversation[];
  histQuery: string;
  setHistQuery: (v: string) => void;
  currentId: string | null;
  renamingId: string | null;
  setRenamingId: (v: string | null) => void;
  renameValue: string;
  setRenameValue: (v: string) => void;
  commitRename: (id: string) => void;
  pinnedIds: string[];
  togglePinned: (id: string) => void;
  startRename: (id: string, title: string) => void;
  selectConversation: (conv: Conversation) => void;
  exportConversation: (conv: ExportConversation, fmt: "md" | "json") => void;
  shareConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
};

export function PlaygroundHistoryPanel({
  historyOpen,
  setHistoryOpen,
  conversations,
  histQuery,
  setHistQuery,
  currentId,
  renamingId,
  setRenamingId,
  renameValue,
  setRenameValue,
  commitRename,
  pinnedIds,
  togglePinned,
  startRename,
  selectConversation,
  exportConversation,
  shareConversation,
  deleteConversation,
}: PlaygroundHistoryPanelProps) {
  // History list: search by title + pinned first, then recent.
  const q = histQuery.trim().toLowerCase();
  const visibleConvs = useMemo(
    () => (historyOpen
      ? conversations
          .filter((c) => !q || (c.title || "").toLowerCase().includes(q))
          .sort((a, b) => {
            const pa = pinnedIds.includes(a.id) ? 0 : 1;
            const pb = pinnedIds.includes(b.id) ? 0 : 1;
            return pa - pb || ((b.ts ?? 0) - (a.ts ?? 0));
          })
      : []),
    [historyOpen, conversations, q, pinnedIds],
  );
  return (
    // ONE row per conversation, trash can at the end. Clicking the row opens
    // the conversation (and closes the panel); clicking the trash can deletes
    // WITHOUT closing, so several can be cleaned up in a row. The trash can is
    // a SIBLING button of the row, not a nested one: no risk of a click
    // triggering both.
    <HistoryDialog
      historyOpen={historyOpen}
      setHistoryOpen={setHistoryOpen}
      count={conversations.length}
      histQuery={histQuery}
      setHistQuery={setHistQuery}
      q={q}
      visibleConvs={visibleConvs}
      currentId={currentId}
      renamingId={renamingId}
      setRenamingId={setRenamingId}
      renameValue={renameValue}
      setRenameValue={setRenameValue}
      commitRename={commitRename}
      pinnedIds={pinnedIds}
      togglePinned={togglePinned}
      startRename={startRename}
      selectConversation={selectConversation}
      exportConversation={exportConversation}
      shareConversation={shareConversation}
      deleteConversation={deleteConversation}
    />
  );
}
