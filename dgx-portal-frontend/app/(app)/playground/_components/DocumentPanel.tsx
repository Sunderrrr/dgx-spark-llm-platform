"use client";

// The playground document side panel (code file / document / live draft) and
// its fullscreen twin. Extracted VERBATIM from app/(app)/playground/page.tsx
// (plain props, no behaviour change): the page keeps the panel model (what is
// shown), the rename/preview state and the stick-to-bottom wiring.


import { ResizeHandle, type ResizableRegion } from "@astryxdesign/core/Resizable";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Toolbar } from "@astryxdesign/core/Toolbar";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import {
  ArrowDownTrayIcon,
  ArrowDownIcon,
  ClipboardDocumentIcon,
  CheckIcon,
  PencilIcon,
  DocumentTextIcon,
  XMarkIcon,
  ArrowsPointingOutIcon,
} from "@heroicons/react/24/outline";
// Wrapper that closes over the dependency's URL filter (see lib/markdown.tsx).
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { useT } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";
import { downloadText } from "@/lib/export";
import type { Artifact } from "@/lib/playground-thread";

/** Everything both panel layouts need (plain data + stable handlers). */
export type DocumentPanelProps = {
  panelIsCode: boolean;
  panelTitle: string;
  panelSubtitle: string;
  panelContent: string;
  panelLang: string;
  panelEstHtml: boolean;
  panelDownloadName: string;
  panelDownloadMime: string;
  showLive: boolean;
  canRenamePanel: boolean;
  renamingArtifact: boolean;
  renameArtifactValue: string;
  setRenamingArtifact: (v: boolean) => void;
  setRenameArtifactValue: (v: string) => void;
  epingle: Artifact | null;
  commitArtifactRename: (a: Artifact, title: string) => void;
  htmlPreview: boolean;
  setHtmlPreview: (v: boolean) => void;
  previewUrl: string;
  erreurApercu: string;
  streaming: boolean;
  corrigerErreur: (nom: string, message: string) => void;
  refApercu: (el: HTMLIFrameElement | null) => void;
  panelScrollRef: (node: HTMLElement | null) => void;
  onPanelScroll: () => void;
  showPanelJump: boolean;
  panelJumpDown: () => void;
  onClose: () => void;
  artifactResize: ResizableRegion;
  setPlein: (v: boolean) => void;
  fermerPanneau: () => void;
};

/** Side panel: pinned beside the chat on wide screens. */
export function DocumentPanel({
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
  erreurApercu,
  streaming,
  corrigerErreur,
  refApercu,
  panelScrollRef,
  onPanelScroll,
  showPanelJump,
  panelJumpDown,
  onClose,
  artifactResize,
  setPlein,
}: DocumentPanelProps) {
  const t = useT();
  const showToast = useToast();
  return (
            <>
              <ResizeHandle
                direction="horizontal"
                resizable={artifactResize.props}
                isReversed
                pillPlacement="start"
                hasDivider
                label={t("Redimensionner le panneau")}
              />
              <Card
                variant="transparent"
                height="100%"
                style={{ width: artifactResize.size, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
                <Toolbar
                  label={panelIsCode ? t("Fichier") : t("Document")}
                  dividers={["bottom"]}
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
                    ) : (
                      <HStack gap={2} vAlign="center">
                        <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                        <VStack gap={0}>
                          <Text weight="semibold">{panelTitle}</Text>
                          {panelSubtitle ? <Text type="supporting" color="secondary">{panelSubtitle}</Text> : null}
                        </VStack>
                      </HStack>
                    )
                  }
                  endContent={
                    renamingArtifact && canRenamePanel ? (
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
                        <Button label={t("Plein écran")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowsPointingOutIcon} size="sm" />}
                          onClick={() => setPlein(true)} />
                        <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                          onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                        <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                          onClick={() => void copierTexte(panelContent, () =>
                            showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))} />
                        <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={XMarkIcon} size="sm" />}
                          onClick={onClose} />
                      </>
                    )
                  }
                />
                {panelEstHtml && (
                  <HStack padding={3}>
                    <SegmentedControl
                      label={t("Affichage")}
                      value={htmlPreview ? "apercu" : "code"}
                      onChange={(v) => setHtmlPreview(v === "apercu")}
                      size="sm">
                      <SegmentedControlItem value="apercu" label={t("Aperçu")} />
                      <SegmentedControlItem value="code" label={t("Code source")} />
                    </SegmentedControl>
                  </HStack>
                )}
                {panelEstHtml && htmlPreview && erreurApercu ? (
                  /* The page opens but raises an error: it is well-formed and
                     yet unusable. Without this banner, the user sees an
                     empty or frozen preview with no idea why. */
                  <HStack padding={3}>
                    <Banner
                      status="warning"
                      title={t("La page ne s'exécute pas")}
                      description={erreurApercu}
                      endContent={
                        <Button
                          label={t("Corriger")}
                          variant="primary"
                          size="sm"
                          isDisabled={streaming}
                          onClick={() => corrigerErreur(panelTitle, erreurApercu)}
                        />
                      }
                    />
                  </HStack>
                ) : null}
                {panelEstHtml && htmlPreview ? (
                  /* Model-generated page, in an ISOLATED iframe.
                     `allow-scripts` WITHOUT `allow-same-origin`: the page can
                     run — otherwise buttons and interactions are dead, which
                     makes the preview useless for an interactive page — but its
                     origin stays OPAQUE. It can therefore neither read session
                     cookies, nor touch the DOM of the hosting page, nor
                     call the API with the user's rights.
                     `allow-same-origin` must NEVER be added here: combined with
                     `allow-scripts`, it cancels the sandbox and a generated HTML
                     would become code executed in our own origin. */
                  <iframe
                    ref={refApercu}
                    title={panelTitle}
                    src={previewUrl}
                    sandbox="allow-scripts allow-forms allow-popups"
                    style={{
                      flex: 1,
                      minHeight: 0,
                      width: "100%",
                      border: "none",
                      background: "var(--color-background-surface)",
                    }}
                  />
                ) : (
                <VStack ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable style={{ flex: 1, minHeight: 0 }}>
                  {/* Valeurs unifiées : pendant le flux il n'y a pas encore
                      d'artefact épinglé, mais bien un fichier à afficher. */}
                  {/* `flexShrink: 0` : SANS ça le CodeBlock rétrécit à la
                      hauteur du corps (ses deux étages sont des items flex en
                      `flex: 0 1 auto`, `min-height` résolu à 0 puisque leur
                      `overflow` n'est pas `visible`) et son PROPRE conteneur
                      interne devient le seul à défiler — le corps, lui, ne
                      déborde jamais : le suivi automatique et le bouton
                      « Descendre », branchés sur le corps, ne servaient à
                      rien. Empêcher le rétrécissement rend au corps sa place
                      d'UNIQUE conteneur de défilement, pour le code comme pour
                      les documents (mesuré : corps 792/792 contre code interne
                      5 716/720). */}
                  {panelIsCode
                    ? <CodeBlock title={panelTitle} language={panelLang} code={panelContent} width="100%" isWrapped container="section" style={{ flexShrink: 0 }} />
                    : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                </VStack>
                )}
                {showPanelJump && (
                  <HStack style={{ position: "absolute", bottom: "var(--spacing-4)", left: "50%", transform: "translateX(-50%)", zIndex: 2 }}>
                    <Button label={t("Descendre")} variant="primary" size="sm"
                      icon={<Icon icon={ArrowDownIcon} size="sm" />}
                      onClick={panelJumpDown} />
                  </HStack>
                )}
              </Card>
            </>
  );
}
