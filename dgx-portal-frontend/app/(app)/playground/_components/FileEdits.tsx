"use client";

// Files the server's edit tools changed during a turn: one file card per
// file (« Modifié +5 −2 »), the red/green diff on demand, and the new version
// one click away. It was a small grey tool line (« edit hello.py +5 -2 »),
// easy to miss under the answer (2026-10-12).

import { useMemo } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { useT } from "@/lib/i18n";
import { diffLines, diffStats, hunks, type DiffLine } from "@/lib/diff";
import type { EditedFile } from "@/lib/types";
import type { Artifact } from "@/lib/playground-thread";
import { FileCard } from "./FileCard";

type Props = {
  edits: EditedFile[];
  /** Content of each file BEFORE this turn ("" for a created file). */
  before: (name: string) => string | undefined;
  lang: (name: string) => string;
  /** Displayed name and version number of a file, as on the other cards. */
  title: (name: string) => string;
  version: (name: string) => number | undefined;
  onOpen: (a: Artifact) => void;
};

export function DiffView({ lines }: { lines: (DiffLine | null)[] }) {
  const t = useT();
  return (
    <VStack gap={0} isScrollable className="file-diff">
      {lines.map((l, k) =>
        l === null ? (
          <Text key={k} type="code" color="secondary" className="file-diff-gap">⋯</Text>
        ) : (
          <HStack key={k} gap={2} className={`file-diff-line file-diff-${l.kind}`}>
            <Text type="code" color="secondary" className="file-diff-no">
              {String(l.kind === "add" ? l.newNo : l.oldNo ?? "")}
            </Text>
            <Text type="code" color="secondary" className="file-diff-sign">
              {l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}
            </Text>
            <Text type="code" className="file-diff-text">{l.text || " "}</Text>
          </HStack>
        ),
      )}
      {lines.length === 0 && <Text type="supporting">{t("Aucune différence.")}</Text>}
    </VStack>
  );
}

export function FileEdits({ edits, before, lang, title, version, onOpen }: Props) {
  const rows = useMemo(
    () =>
      edits.map((f) => {
        const old = before(f.name);
        // A created file has no « before »: all of it is added (diffing it
        // against "" would count one removed empty line).
        const lines: DiffLine[] = old === undefined
          ? f.content.split("\n").map((text, k) => ({ kind: "add" as const, text, newNo: k + 1 }))
          : diffLines(old, f.content);
        return { f, created: old === undefined, lines, stats: diffStats(lines) };
      }),
    // Keyed on `edits` only: `before` is a new closure on every render (the
    // thread re-renders per streamed token) but answers the same for a
    // finished message, and the diff of a long file is worth not redoing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [edits],
  );
  return (
    <VStack gap={2}>
      {rows.map(({ f, created, lines, stats }) => {
        const artifact: Artifact = { kind: "code", title: f.name, lang: lang(f.name), content: f.content };
        return (
          <FileCard
            key={f.name}
            artifact={artifact}
            title={title(f.name)}
            version={version(f.name)}
            change={{ created, added: stats.added, removed: stats.removed,
              lines: created ? lines.slice(0, 400) : hunks(lines) }}
            onOpen={() => onOpen(artifact)}
          />
        );
      })}
    </VStack>
  );
}
