"use client";

// The log viewer: live SSE stream of the chat model, tails of the sidecars on
// demand. The viewer itself is untouched (one CodeBlock + the source
// SegmentedControl + stick-to-bottom); the block is now FOLDABLE as a whole
// (the logs are big and the operator sometimes wants the rest of the tab),
// and carries a « Copier » button that sends the logs to the clipboard —
// operators paste them into the Support chat. The copy goes through
// lib/copier.ts, whose fallback survives a plain-HTTP LAN (no Clipboard API).

import { useCallback, useMemo, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowDownIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  ClipboardDocumentIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { copierTexte } from "@/lib/copier";

export type LogKind = "llm" | "ocr" | "voice" | "asr" | "image" | "music" | "video";

export function LogsPanel({ model, logKind, onLogKindChange, logs, sidecarLogs }: {
  /** Served chat model, for the title of the "llm" tab. */
  model: string | null;
  logKind: LogKind;
  onLogKindChange: (kind: LogKind) => void;
  logs: string[];
  sidecarLogs: string[];
}) {
  const t = useT();
  const showToast = useToast();
  // Folded as a whole: the panel is tall, and the operator reads it when they
  // diagnose — otherwise it should let the rest of the tab breathe.
  const [isFolded, setIsFolded] = useState(false);

  // Text displayed in the log viewer, and automatic stick-to-bottom —
  // same behaviour as the Playground panel: stick to the bottom as long as
  // the admin has not scrolled up; if they do, we stop pulling them back down and
  // an arrow appears to go back down and re-enable the tracking. `active` is
  // always true here (unlike the Playground where it only tracks during the
  // stream): logs keep arriving as long as the page is open.
  // The join of up to 600 lines was redone at EVERY render — including an
  // unrelated keystroke or an unchanged poll.
  const logText = useMemo(
    () => (logKind === "llm" ? logs : sidecarLogs).join("\n"),
    [logKind, logs, sidecarLogs],
  );
  const {
    setRef: attachLogsScroller,
    showButton: showLogsJump,
    scrollToBottom: logsJumpDown,
  } = useStickToBottom(logText, true);

  // CodeBlock manages its own scrolling (as soon as it is given a maxHeight) and
  // does not expose this container. So we put the ref on a parent and go down
  // to the actually scrollable element — verified in the browser: without this, the
  // text is clipped by an overflow:hidden child and nothing scrolls anymore.
  const setLogsScrollRef = useCallback(
    (node: HTMLElement | null) => {
      if (!node) return attachLogsScroller(null);
      const scroller =
        Array.from(node.querySelectorAll<HTMLElement>("*")).find((e) => {
          const o = getComputedStyle(e).overflowY;
          return o === "auto" || o === "scroll";
        }) ?? node;
      attachLogsScroller(scroller);
    },
    [attachLogsScroller],
  );

  function copyLogs() {
    if (!logText) return;
    void copierTexte(logText, () =>
      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))
      .then((ok) => {
        if (ok) showToast({ body: t("Copié."), type: "info" });
      });
  }

  return (
    <Card>
      <VStack gap={2}>
        <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
          <HStack gap={2} vAlign="center">
            {/* The chevron folds the WHOLE block (viewer + source selector):
                visible again with the same state, nothing is re-fetched. */}
            <Button
              label={isFolded ? t("Tout déplier") : t("Tout replier")}
              variant="ghost"
              size="sm"
              icon={<Icon icon={isFolded ? ChevronDownIcon : ChevronUpIcon} size="sm" />}
              onClick={() => setIsFolded((f) => !f)}
            />
            <Text weight="semibold">
              {logKind === "llm"
                ? t("Logs — {v}").replace("{v}", model || t("aucun modèle"))
                : t("Logs — {v}").replace("{v}", logKind.toUpperCase())}
            </Text>
          </HStack>
          <HStack gap={2} vAlign="center">
            <SegmentedControl label={t("Logs à afficher")} value={logKind} onChange={(v) => onLogKindChange(v as LogKind)}>
              <SegmentedControlItem value="llm" label={t("LLM")} />
              <SegmentedControlItem value="ocr" label="OCR" />
              <SegmentedControlItem value="voice" label={t("Voix")} />
              <SegmentedControlItem value="asr" label={t("Dictée")} />
              <SegmentedControlItem value="image" label={t("Image")} />
              <SegmentedControlItem value="music" label={t("Musique")} />
              <SegmentedControlItem value="video" label={t("Vidéo")} />
            </SegmentedControl>
            <Button
              label={t("Copier")}
              variant="secondary"
              size="sm"
              icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
              isDisabled={!logText}
              onClick={copyLogs}
            />
          </HStack>
        </HStack>
        {!isFolded && (
          <>
            {/* The maxHeight hands the scrolling to CodeBlock: constraining a
                parent instead does not work, CodeBlock gets compressed and
                clips its text (internal overflow:hidden), so that nothing
                overflows or scrolls anymore. The ref only serves as an entry
                point to find its scroller. The container is position
                relative to anchor the arrow INSIDE the frame. */}
            <VStack ref={setLogsScrollRef} style={{ position: "relative" }}>
              <CodeBlock
                code={logText || t("Aucun log — ce modèle n'est pas démarré.")}
                language="plaintext"
                hasCopyButton
                width="100%"
                maxHeight={280}
              />
              {/* Arrow alone, centered at the bottom of the frame (the top-right
                  corner is already taken by the Copier button of CodeBlock). */}
              {showLogsJump && (
                <HStack
                  style={{
                    position: "absolute",
                    bottom: "var(--spacing-3)",
                    left: "50%",
                    transform: "translateX(-50%)",
                    zIndex: 2,
                  }}
                >
                  <Button
                    label={t("Descendre")}
                    variant="primary"
                    size="sm"
                    isIconOnly
                    icon={<Icon icon={ArrowDownIcon} size="sm" />}
                    onClick={logsJumpDown}
                  />
                </HStack>
              )}
            </VStack>
          </>
        )}
      </VStack>
    </Card>
  );
}
