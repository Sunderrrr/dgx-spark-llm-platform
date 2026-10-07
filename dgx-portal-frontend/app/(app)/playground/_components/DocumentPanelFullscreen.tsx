"use client";

// Fullscreen twin of the document panel (phones, or the « Plein écran »
// button). Extracted VERBATIM from app/(app)/playground/page.tsx — same props
// as DocumentPanel, no behaviour change.

import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useToast } from "@astryxdesign/core/Toast";
import { HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { TextInput } from "@astryxdesign/core/TextInput";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import {
  ArrowDownTrayIcon,
  ArrowDownIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  PencilIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
// Wrapper that closes over the dependency's URL filter (see lib/markdown.tsx).
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { useT } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";
import { downloadText } from "@/lib/export";
import type { DocumentPanelProps } from "./DocumentPanel";

export function DocumentPanelFullscreen({
  panelIsCode,
  panelTitle,
  panelSubtitle,
  panelContent,
  panelLang,
  panelEstHtml,
  panelDownloadName,
  panelDownloadMime,
  showLive,
  canRenamePanel,
  renamingArtifact,
  renameArtifactValue,
  setRenamingArtifact,
  setRenameArtifactValue,
  epingle,
  commitArtifactRename,
  htmlPreview,
  setHtmlPreview,
  previewUrl,
  refApercu,
  panelScrollRef,
  onPanelScroll,
  showPanelJump,
  panelJumpDown,
  fermerPanneau,
}: DocumentPanelProps) {
  const t = useT();
  const showToast = useToast();
  return (
            <Dialog isOpen onOpenChange={(o) => { if (!o) fermerPanneau(); }} variant="fullscreen">
              <Layout
                header={
                  <DialogHeader
                    title={panelTitle}
                    subtitle={panelSubtitle || undefined}
                    hasDivider
                    onOpenChange={(o) => { if (!o) fermerPanneau(); }}
                    endContent={
                      <HStack gap={1} vAlign="center">
                        {renamingArtifact && canRenamePanel ? (
                          <>
                            <Button label={t("Valider")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={CheckIcon} size="sm" />}
                              onClick={() => epingle && commitArtifactRename(epingle, renameArtifactValue)} />
                            <Button label={t("Annuler")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={XMarkIcon} size="sm" />}
                              onClick={() => setRenamingArtifact(false)} />
                          </>
                        ) : (
                          <>
                            {canRenamePanel && (
                              <Button label={t("Renommer ce fichier")} variant="ghost" size="sm" isIconOnly
                                icon={<Icon icon={PencilIcon} size="sm" />}
                                onClick={() => { setRenameArtifactValue(panelTitle); setRenamingArtifact(true); }} />
                            )}
                            <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                              onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                            <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                              icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                              onClick={() => void copierTexte(panelContent, () =>
                            showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))} />
                          </>
                        )}
                      </HStack>
                    }
                    startContent={
                      renamingArtifact && canRenamePanel ? (
                        <TextInput
                          label={t("Nouveau nom du fichier")}
                          value={renameArtifactValue}
                          onChange={setRenameArtifactValue}
                          onEnter={() => epingle && commitArtifactRename(epingle, renameArtifactValue)}
                          isLabelHidden
                          size="sm"
                        />
                      ) : panelEstHtml ? (
                        <SegmentedControl
                          label={t("Affichage")}
                          value={htmlPreview ? "apercu" : "code"}
                          onChange={(v) => setHtmlPreview(v === "apercu")}
                          size="sm">
                          <SegmentedControlItem value="apercu" label={t("Aperçu")} />
                          <SegmentedControlItem value="code" label={t("Code source")} />
                        </SegmentedControl>
                      ) : undefined
                    }
                  />
                }
                content={
                  panelEstHtml && htmlPreview ? (
                    /* Same isolated preview as in the panel: full screen is meant
                       precisely to WATCH the page, not re-read its code. */
                    <iframe
                      ref={refApercu}
                      title={panelTitle}
                      src={previewUrl}
                      sandbox="allow-scripts allow-forms allow-popups"
                      style={{ width: "100%", height: "100%", border: "none",
                               background: "var(--color-background-surface)" }}
                    />
                  ) : (
                  <LayoutContent ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable>
                    {panelIsCode
                      ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped container="section" style={{ flexShrink: 0 }} />
                      : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                  </LayoutContent>
                  )
                }
              />
              {showPanelJump && (
                <HStack style={{ position: "fixed", bottom: "var(--spacing-6)", left: "50%", transform: "translateX(-50%)", zIndex: 10 }}>
                  <Button label={t("Descendre")} variant="primary" size="sm"
                    icon={<Icon icon={ArrowDownIcon} size="sm" />}
                    onClick={panelJumpDown} />
                </HStack>
              )}
            </Dialog>
  );
}
