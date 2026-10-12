"use client";

// « Fichiers (N) »: every file of the conversation in one place — its latest
// version, how many versions it went through — and the whole set as one
// .zip. Before, a file was only reachable by scrolling back to the message
// that produced it (2026-10-12).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { ArrowDownTrayIcon, ArchiveBoxArrowDownIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { downloadBlob, downloadText } from "@/lib/export";
import { zipFiles } from "@/lib/zip";
import { downloadName, mimeForLanguage, slugify, type Artifact, type FileVersion } from "@/lib/playground-thread";
import { fileIcon } from "./FileCard";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  versions: Map<string, FileVersion[]>;
  /** Displayed name (user renames win). */
  title: (name: string) => string;
  /** Archive name: the conversation's title. */
  archiveName: string;
  onOpen: (a: Artifact) => void;
};

export function FilesDialog({ isOpen, onClose, versions, title, archiveName, onOpen }: Props) {
  const t = useT();
  const files = [...versions].map(([name, list]) => {
    const last = list[list.length - 1];
    const artifact: Artifact = { kind: "code", title: name, lang: last.lang, content: last.content };
    return { name, artifact, count: list.length, shown: title(name) };
  });
  const downloadAll = () => {
    const bytes = zipFiles(files.map((f) => ({ name: downloadName(f.shown, f.artifact.kind === "code" ? f.artifact.lang : ""), content: f.artifact.content })));
    downloadBlob(`${slugify(archiveName) || "fichiers"}.zip`, new Blob([bytes as BlobPart], { type: "application/zip" }));
  };
  return (
    <Dialog isOpen={isOpen} onOpenChange={(o) => { if (!o) onClose(); }} width={560}>
      <DialogHeader
        title={t("Fichiers")}
        subtitle={(files.length > 1 ? t("{n} fichiers dans cette conversation") : t("{n} fichier dans cette conversation"))
          .replace("{n}", String(files.length))}
        hasDivider
        onOpenChange={(o) => { if (!o) onClose(); }}
      />
      <VStack padding={3} gap={3}>
        {files.length === 0 ? (
          <Text color="secondary">{t("Aucun fichier pour l'instant : ceux que le modèle écrit apparaîtront ici.")}</Text>
        ) : (
          <>
            <VStack gap={0} isScrollable style={{ maxHeight: "60vh" }}>
              <List hasDividers density="compact">
                {files.map((f) => {
                  const lines = f.artifact.content.split("\n").length;
                  const meta = [
                    f.artifact.kind === "code" ? f.artifact.lang : t("Document"),
                    (lines > 1 ? t("{n} lignes") : t("{n} ligne")).replace("{n}", String(lines)),
                    ...(f.count > 1 ? [t("{n} versions").replace("{n}", String(f.count))] : []),
                  ].join(" · ");
                  return (
                    <ListItem
                      key={f.name}
                      label={f.shown}
                      description={meta}
                      startContent={<Icon icon={fileIcon(f.artifact)} size="md" color="accent" />}
                      onClick={() => { onOpen(f.artifact); onClose(); }}
                      endContent={
                        <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                          onClick={(e) => {
                            e.stopPropagation();
                            const lang = f.artifact.kind === "code" ? f.artifact.lang : "";
                            downloadText(downloadName(f.shown, lang), f.artifact.content, mimeForLanguage(lang));
                          }} />
                      }
                    />
                  );
                })}
              </List>
            </VStack>
            {files.length > 1 ? (
              <HStack hAlign="end">
                <Button label={t("Tout télécharger (.zip)")} variant="secondary" size="sm"
                  icon={<Icon icon={ArchiveBoxArrowDownIcon} size="sm" />}
                  onClick={downloadAll} />
              </HStack>
            ) : null}
          </>
        )}
      </VStack>
    </Dialog>
  );
}
