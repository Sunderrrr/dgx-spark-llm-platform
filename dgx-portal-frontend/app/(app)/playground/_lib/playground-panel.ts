// What the playground's document panel displays, derived from the page's
// state. Extracted VERBATIM from PlaygroundPage.tsx (pure derivation, no
// behaviour change): the page keeps the state, the handlers and the effect
// that publishes an HTML page to the preview — only the computation moved.
//
// No state is held here and nothing is memoized: the two things that MUST stay
// memoized in the page (`versions`, `changeLines`) keep their `useMemo` there,
// because `versions` is also read by the message cards and the header.
//
// The precedence is deliberate: the file being written > the last finished
// file > the document being written > the pinned artifact. Read it top to
// bottom, each value builds on the previous one.

import type { ChatMsg } from "@/lib/types";
import type { Artifact, FileVersion } from "@/lib/playground-thread";
import {
  docTitleFromContent,
  downloadName,
  isDocTask,
  mimeForLanguage,
  parseArtifacts,
  slugify,
  titleFromContext,
} from "@/lib/playground-thread";
import { openCodeFence } from "@/lib/playground-parsers";
import type { LiveCode } from "../_components/ThreadMessage";

export type PanelChoice = { of: Artifact | null; idx: number; changes: boolean };

export type PanelInput = {
  messages: ChatMsg[];
  streaming: boolean;
  isNarrow: boolean;
  artifact: Artifact | null;
  /** The user asked to watch the in-progress document. */
  liveDocOpen: boolean;
  /** The panel's version picker / « Changements » view. */
  panelChoice: PanelChoice;
  /** Every version of every file, as computed by the page (useMemo). */
  versions: Map<string, FileVersion[]>;
  t: (s: string) => string;
  /** Display name of a file (renames are per conversation). */
  renamedTitle: (a: Artifact) => string;
};

export type PanelView = {
  liveCode: LiveCode | null;
  lastFinished: Artifact | null;
  showLive: boolean;
  panelIsCode: boolean;
  panelTitle: string;
  panelSubtitle: string;
  panelContent: string;
  panelLang: string;
  panelIsHtml: boolean;
  panelDownloadName: string;
  panelDownloadMime: string;
  canRenamePanel: boolean;
  pinned: Artifact | null;
  /** The version/changes choice, kept only while it is about `pinned`. */
  chosen: PanelChoice | null;
  pinnedVersions: FileVersion[] | undefined;
  versionIdx: number;
  showChanges: boolean;
};

export function derivePanel({
  messages,
  streaming,
  isNarrow,
  artifact,
  liveDocOpen,
  panelChoice,
  versions,
  t,
  renamedTitle,
}: PanelInput): PanelView {
  const lastMsg = messages[messages.length - 1];
  // While a document is being streamed, the chat shows a card (not the raw text)
  // and the side panel can show a live view of the document being written.
  const streamingDocActive =
    streaming && lastMsg?.role === "assistant" && isDocTask(messages[messages.length - 2]?.content ?? "");
  const liveContent = streamingDocActive ? (lastMsg?.content ?? "") : "";
  // Code file being written: it displays DIRECTLY in the panel,
  // not in the chat. On a narrow screen there is no panel — we then let it
  // scroll in the chat, otherwise the user would see nothing being written.
  const liveCode =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? openCodeFence(lastMsg.content ?? "")
      : null;
  const liveCodeTitle = liveCode
    ? titleFromContext((lastMsg?.content ?? "").slice(0, liveCode.start)) || `${liveCode.lang}`
    : "";
  const showLiveDoc = liveDocOpen && streamingDocActive;
  // Between two files (previous block closed, next not started), the panel
  // keeps the last finished file instead of emptying. Derived from content, no
  // state: nothing to synchronize, hence nothing to desynchronize.
  const lastFinished =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? (parseArtifacts(lastMsg.content ?? "", false, t, messages[messages.length - 2]?.content ?? "").artifacts.slice(-1)[0] ?? null)
      : null;
  const pinned = liveCode ? null : (lastFinished ?? artifact);
  const showLive = showLiveDoc || !!liveCode;
  // Unified panel values: file being written, last finished file, live document,
  // else the pinned artifact.
  const panelIsCode = !!liveCode || pinned?.kind === "code";
  const panelTitle = liveCode
    ? liveCodeTitle
    : showLiveDoc
      ? docTitleFromContent(liveContent)
      : (pinned ? renamedTitle(pinned) : "Document");
  const canRenamePanel = !!pinned && !liveCode && !showLiveDoc;
  // Versions of the pinned file (the panel's picker). The choice is stored
  // WITH the artifact it was made on: opening another file falls back to the
  // version that file's card showed, with nothing to reset.
  const pinnedVersions = pinned?.kind === "code" && !showLive ? versions.get(pinned.title) : undefined;
  const pinnedMatch = pinnedVersions ? pinnedVersions.findIndex((v) => v.content === pinned!.content) : -1;
  const chosen = panelChoice.of === pinned && pinnedVersions ? panelChoice : null;
  const versionIdx = chosen ? Math.min(chosen.idx, pinnedVersions!.length - 1) : pinnedMatch;
  const shownVersion = versionIdx >= 0 ? pinnedVersions?.[versionIdx] : undefined;
  const showChanges = !!chosen?.changes && versionIdx > 0;
  const panelContent = liveCode
    ? liveCode.body
    : showLiveDoc
      ? liveContent
      : (shownVersion?.content ?? pinned?.content ?? "");
  const panelSubtitle = liveCode
    ? t("Écriture en cours…")
    : showLiveDoc
      ? t("Rédaction en cours…")
      : (panelIsCode && pinned?.kind === "code" ? pinned.lang : "");
  const panelLang = liveCode ? liveCode.lang : (pinned?.kind === "code" ? pinned.lang : "text");
  // A finished HTML page can be WATCHED, not only read as code. We do not
  // offer it while it is being written: a half-written render flickers.
  const panelIsHtml = !liveCode && panelIsCode && /^html?$/i.test(panelLang);
  const panelDownloadName = panelIsCode
    ? downloadName(panelTitle, panelLang)
    : `${slugify(panelTitle)}.md`;
  const panelDownloadMime = panelIsCode ? mimeForLanguage(panelLang) : "text/markdown";
  return {
    liveCode,
    lastFinished,
    showLive,
    panelIsCode,
    panelTitle,
    panelSubtitle,
    panelContent,
    panelLang,
    panelIsHtml,
    panelDownloadName,
    panelDownloadMime,
    canRenamePanel,
    pinned,
    chosen,
    pinnedVersions,
    versionIdx,
    showChanges,
  };
}
