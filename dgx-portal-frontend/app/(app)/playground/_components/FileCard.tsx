"use client";

// A file of the conversation, shown where its block was in the answer.
// « pas assez visible » (2026-10-12): the old card was a grey strip with a
// name and a language, piled at the end of the message. This one says what
// the file is (type, size, version, what changed), shows its first lines and
// gives the three actions people use without opening the panel.

import { useState } from "react";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowDownTrayIcon,
  ClipboardDocumentIcon,
  CodeBracketIcon,
  DocumentTextIcon,
  GlobeAltIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { copyText } from "@/lib/clipboard";
import { downloadText } from "@/lib/export";
import { downloadName, mimeForLanguage, slugify, type Artifact } from "@/lib/playground-thread";
import type { DiffLine } from "@/lib/diff";
import { DiffView } from "./FileEdits";

/** Lines shown under the name: enough to recognize the file. */
const PREVIEW_LINES = 4;

export type FileChange = {
  created: boolean;
  added: number;
  removed: number;
  /** Diff to show on demand (hunks of a modification, or the created file). */
  lines: (DiffLine | null)[];
};

type Props = {
  artifact: Artifact;
  /** Displayed name (a user rename wins over the model's). */
  title: string;
  /** « v2 » when the file has several versions in the conversation. */
  version?: number;
  /** Set when the platform's edit tools changed the file in this turn. */
  change?: FileChange;
  onOpen: () => void;
};

export function fileIcon(a: Artifact) {
  if (a.kind === "doc") return DocumentTextIcon;
  return a.lang === "html" || /\.html?$/i.test(a.title) ? GlobeAltIcon : CodeBracketIcon;
}

export function FileCard({ artifact, title, version, change, onOpen }: Props) {
  const t = useT();
  const showToast = useToast();
  const [diffOpen, setDiffOpen] = useState(false);
  const isCode = artifact.kind === "code";
  const lineCount = artifact.content.split("\n").length;
  const preview = artifact.content
    .split("\n")
    .filter((l, k, all) => l.trim() || (k > 0 && all[k - 1].trim()))
    .slice(0, PREVIEW_LINES)
    .join("\n");
  const fileName = isCode ? downloadName(title, artifact.lang) : `${slugify(title)}.md`;
  const mime = isCode ? mimeForLanguage(artifact.lang) : "text/markdown";
  const meta = [
    isCode ? artifact.lang : t("Document"),
    (lineCount > 1 ? t("{n} lignes") : t("{n} ligne")).replace("{n}", String(lineCount)),
  ].join(" · ");

  return (
    <ClickableCard
      label={`${t("Ouvrir le fichier")} ${title}`}
      variant="default"
      padding={0}
      onClick={onOpen}>
      <VStack gap={0}>
        <HStack gap={3} vAlign="center" padding={3}>
          <Icon icon={fileIcon(artifact)} size="md" color="accent" />
          <StackItem size="fill">
            <VStack gap={0}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold" maxLines={1}>{title}</Text>
                {change ? (
                  <Badge variant={change.created ? "success" : "info"}
                    label={change.created ? t("Créé") : t("Modifié")} />
                ) : null}
                {version && version > 1 ? <Badge variant="neutral" label={`v${version}`} /> : null}
              </HStack>
              <HStack gap={2} vAlign="center">
                <Text type="supporting" color="secondary">{meta}</Text>
                {change && !change.created ? (
                  <>
                    <Text type="supporting" className="file-card-add">+{change.added}</Text>
                    <Text type="supporting" className="file-card-del">−{change.removed}</Text>
                  </>
                ) : null}
              </HStack>
            </VStack>
          </StackItem>
          <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
            icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
            onClick={() => void copyText(artifact.content,
              () => showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))
              .then((ok) => ok && showToast({ body: t("Copié"), type: "info" }))} />
          <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
            icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
            onClick={() => downloadText(fileName, artifact.content, mime)} />
          <Button label={t("Ouvrir")} variant="secondary" size="sm" onClick={onOpen} />
        </HStack>
        {change && !change.created ? (
          <VStack gap={2} padding={3} className="file-card-preview">
            <Button
              label={diffOpen ? t("Masquer les changements") : t("Voir les changements")}
              variant="ghost"
              size="sm"
              onClick={() => setDiffOpen((v) => !v)}
            />
            {diffOpen ? <DiffView lines={change.lines} /> : null}
          </VStack>
        ) : preview.trim() ? (
          <Text type="code" color="secondary" className="file-card-preview file-card-code">
            {preview}
          </Text>
        ) : null}
      </VStack>
    </ClickableCard>
  );
}
