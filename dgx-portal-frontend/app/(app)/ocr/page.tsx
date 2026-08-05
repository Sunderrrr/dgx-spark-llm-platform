"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { FileInput } from "@astryxdesign/core/FileInput";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Badge } from "@astryxdesign/core/Badge";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { useToast } from "@astryxdesign/core/Toast";
import {
  DocumentTextIcon,
  DocumentMagnifyingGlassIcon,
  MoonIcon,
  ClipboardIcon,
  XMarkIcon,
  PhotoIcon,
  ViewfinderCircleIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { streamOcr } from "@/lib/api";

type HistoryItem = { id: number; text: string; created_at: string; has_image: boolean };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };
type OcrBlock = { label: string; coords: [number, number, number, number] | null; text: string };

// Deux modèles, deux formats de sortie totalement différents pour les zones
// détectées — le format est auto-détecté sur le texte reçu (le modèle
// réellement actif peut changer via le catalogue OCR admin) :
//
// - baidu/Unlimited-OCR : lignes "<label> [x1, y1, x2, y2]texte", virgules,
//   coordonnées normalisées 0-1000.
// - datalab-to/chandra-ocr-2 : HTML avec des blocs
//   <div data-label="..." data-bbox="x0 y0 x1 y1">...</div>, bbox séparée par
//   des ESPACES (pas des virgules), même normalisation 0-1000. On extrait le
//   textContent (jamais innerHTML) : le résultat n'est donc jamais injecté
//   comme HTML dans la page, juste du texte, même si le modèle produit du
//   HTML malformé ou volontairement piégé (image contenant du texte "<script>…").
const UNLIMITED_OCR_LINE_RE = /^(\S+)\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\](.*)$/;

function parseUnlimitedOcrBlocks(raw: string): OcrBlock[] {
  return raw
    .split("\n")
    .filter((line) => line.length > 0)
    .map((line) => {
      const m = line.match(UNLIMITED_OCR_LINE_RE);
      if (!m) return { label: "", coords: null, text: line } as OcrBlock;
      const [, label, x1, y1, x2, y2, text] = m;
      return {
        label,
        coords: [Number(x1), Number(y1), Number(x2), Number(y2)],
        text: text.trim(),
      };
    });
}

function parseChandraBlocks(html: string): OcrBlock[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const nodes = Array.from(doc.body.querySelectorAll(":scope > div[data-bbox]"));
  return nodes.map((el) => {
    const bbox = (el.getAttribute("data-bbox") || "").trim().split(/\s+/).map(Number);
    const coords: OcrBlock["coords"] =
      bbox.length === 4 && bbox.every((n) => Number.isFinite(n))
        ? (bbox as [number, number, number, number])
        : null;
    return {
      label: (el.getAttribute("data-label") || "").toLowerCase(),
      coords,
      text: (el.textContent || "").trim(),
    };
  });
}

function parseOcrBlocks(raw: string): OcrBlock[] {
  if (!raw) return [];
  return raw.includes("data-bbox=") ? parseChandraBlocks(raw) : parseUnlimitedOcrBlocks(raw);
}

/** Aperçu court pour l'historique : texte lisible, jamais le HTML/les coordonnées brutes. */
function ocrPreviewText(raw: string): string {
  return parseOcrBlocks(raw)
    .map((b) => b.text)
    .filter(Boolean)
    .join(" ")
    .slice(0, 80);
}

const LABEL_BADGE: Record<string, "purple" | "blue" | "teal" | "orange" | undefined> = {
  title: "purple",
  "section-header": "purple",
  header: "blue",
  "page-header": "blue",
  table: "teal",
  "table-of-contents": "teal",
  image: "orange",
  figure: "orange",
  diagram: "orange",
};
const LABEL_COLOR: Record<string, string> = {
  title: "#a855f7",
  "section-header": "#a855f7",
  header: "#3b82f6",
  "page-header": "#3b82f6",
  table: "#14b8a6",
  "table-of-contents": "#14b8a6",
  image: "#f97316",
  figure: "#f97316",
  diagram: "#f97316",
  text: "#22c55e",
};

export default function OcrPage() {
  const csrf = useCsrf();
  const showToast = useToast();
  const [image, setImage] = useState<File | null>(null);
  const [instruction, setInstruction] = useState("document parsing.");
  const [isLoading, setIsLoading] = useState(false);
  const [text, setText] = useState("");
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [ocrModel, setOcrModel] = useState<string | null>(null);
  const [resultView, setResultView] = useState<"text" | "boxes">("text");
  const [resultImageUrl, setResultImageUrl] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const resultImageBlobRef = useRef<string | null>(null);

  const previewUrl = useMemo(() => (image ? URL.createObjectURL(image) : null), [image]);
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  // resultImageUrl (montré dans le panneau de droite) est volontairement
  // indépendant de previewUrl (widget d'upload à gauche) : sinon choisir une
  // nouvelle image pendant qu'un ancien résultat est affiché révoquerait le
  // blob dont la vue « zones détectées » a encore besoin.
  useEffect(() => () => {
    if (resultImageBlobRef.current) URL.revokeObjectURL(resultImageBlobRef.current);
  }, []);

  function setResultImage(url: string | null, isBlob: boolean) {
    if (resultImageBlobRef.current) URL.revokeObjectURL(resultImageBlobRef.current);
    resultImageBlobRef.current = isBlob ? url : null;
    setResultImageUrl(url);
  }

  const blocks = useMemo(() => parseOcrBlocks(text), [text]);
  const boxedBlocks = useMemo(() => blocks.filter((b) => b.coords), [blocks]);

  function loadHistory() {
    fetch("/api/ocr/history", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setHistory)
      .catch(() => {});
  }

  useEffect(loadHistory, []);

  useEffect(() => {
    fetch("/api/home", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const ocr = d?.running_models?.find((m: RunningModel) => m.kind === "ocr");
        setAvailable(!!ocr);
        setOcrModel(ocr?.name ?? null);
      })
      .catch(() => setAvailable(null));
  }, []);

  const isChandra = !!ocrModel?.toLowerCase().includes("chandra");

  async function extract() {
    if (!image || !csrf) return;
    setIsLoading(true);
    setText("");
    setResultView("text");
    setResultImage(URL.createObjectURL(image), true);
    abortRef.current = new AbortController();
    try {
      await streamOcr(csrf, { image, instruction }, abortRef.current.signal, (chunk) => {
        setText((prev) => prev + chunk);
      });
      loadHistory();
    } catch {
      showToast({ body: "OCR injoignable.", type: "error" });
    } finally {
      setIsLoading(false);
    }
  }

  function copyResult() {
    navigator.clipboard.writeText(text).then(() => showToast({ body: "Copié.", type: "info" }));
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable={available === false}>
          {available === false ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title="Aucun modèle OCR n'est disponible"
              description="Demande à un admin de démarrer un modèle OCR pour utiliser cette page."
            />
          ) : (
            <HStack gap={5} height="100%">
              <StackItem>
                <VStack gap={5} width={360}>
                  <VStack gap={1}>
                    <Heading level={1}>OCR</Heading>
                    <Text type="supporting" color="secondary">
                      Extrait le texte d&apos;une image ou d&apos;un document scanné.
                    </Text>
                  </VStack>

                  <Card>
                    <VStack gap={4}>
                      {previewUrl ? (
                        <VStack gap={2}>
                          <AspectRatio ratio={4 / 3} fit="cover">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={previewUrl} alt="Aperçu du document" />
                          </AspectRatio>
                          <Button
                            label="Changer d'image"
                            variant="ghost"
                            size="sm"
                            icon={<Icon icon={XMarkIcon} size="sm" />}
                            isDisabled={isLoading}
                            onClick={() => setImage(null)}
                          />
                        </VStack>
                      ) : (
                        <FileInput
                          label="Image ou scan"
                          value={image}
                          onChange={(f) => setImage(f as File | null)}
                          accept="image/png,image/jpeg,image/webp"
                          maxSize={15 * 1024 * 1024}
                          mode="dropzone"
                          description="PNG, JPEG ou WebP — 15 Mo max."
                          isDisabled={isLoading}
                          isRequired
                        />
                      )}
                      {!isChandra && (
                        <TextInput
                          label="Instruction (optionnel)"
                          value={instruction}
                          onChange={setInstruction}
                          placeholder="document parsing."
                          isDisabled={isLoading}
                        />
                      )}
                      <Button
                        label="Extraire le texte"
                        variant="primary"
                        onClick={extract}
                        isDisabled={!image || isLoading}
                        isLoading={isLoading}
                      />
                    </VStack>
                  </Card>

                  {history.length > 0 && (
                    <VStack gap={2}>
                      <Text type="supporting" color="secondary">
                        Historique ({history.length} dernier{history.length > 1 ? "s" : ""})
                      </Text>
                      <VStack gap={0}>
                        {history.map((h) => (
                          <Item
                            key={h.id}
                            label={ocrPreviewText(h.text) || "(vide)"}
                            labelLines={1}
                            description={new Date(h.created_at).toLocaleString("fr-FR")}
                            startContent={
                              h.has_image ? (
                                // eslint-disable-next-line @next/next/no-img-element
                                <img
                                  src={`/ocr/image/${h.id}`}
                                  alt=""
                                  width={32}
                                  height={32}
                                  style={{ objectFit: "cover", borderRadius: "var(--radius-sm)" }}
                                />
                              ) : (
                                <DocumentTextIcon width={20} height={20} />
                              )
                            }
                            onClick={() => {
                              setText(h.text);
                              setResultView("text");
                              setResultImage(h.has_image ? `/ocr/image/${h.id}` : null, false);
                            }}
                          />
                        ))}
                      </VStack>
                    </VStack>
                  )}
                </VStack>
              </StackItem>

              <StackItem size="fill" isScrollable>
                {text ? (
                  <Card>
                    <VStack gap={3}>
                      <HStack hAlign="between" vAlign="center">
                        <StatusDot
                          variant={isLoading ? "accent" : "success"}
                          label={isLoading ? "Extraction en cours…" : "Terminé"}
                        />
                        <HStack gap={2} vAlign="center">
                          {resultImageUrl && boxedBlocks.length > 0 && (
                            <SegmentedControl label="Vue du résultat" value={resultView} onChange={(v) => setResultView(v as "text" | "boxes")}>
                              <SegmentedControlItem value="text" label="Texte" icon={<Icon icon={DocumentTextIcon} size="sm" />} />
                              <SegmentedControlItem value="boxes" label="Zones détectées" icon={<Icon icon={ViewfinderCircleIcon} size="sm" />} />
                            </SegmentedControl>
                          )}
                          <Button
                            label="Copier"
                            variant="ghost"
                            size="sm"
                            icon={<Icon icon={ClipboardIcon} size="sm" />}
                            onClick={copyResult}
                          />
                        </HStack>
                      </HStack>

                      {resultView === "boxes" && resultImageUrl && boxedBlocks.length > 0 ? (
                        <div style={{ position: "relative", width: "100%", lineHeight: 0 }}>
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img src={resultImageUrl} alt="Zones détectées" style={{ width: "100%", height: "auto", display: "block", borderRadius: "var(--radius-md)" }} />
                          {boxedBlocks.map((b, i) => {
                            const [x1, y1, x2, y2] = b.coords!;
                            const color = LABEL_COLOR[b.label] ?? "#64748b";
                            return (
                              <div
                                key={i}
                                title={`${b.label}${b.text ? `: ${b.text}` : ""}`}
                                style={{
                                  position: "absolute",
                                  left: `${(x1 / 1000) * 100}%`,
                                  top: `${(y1 / 1000) * 100}%`,
                                  width: `${((x2 - x1) / 1000) * 100}%`,
                                  height: `${((y2 - y1) / 1000) * 100}%`,
                                  border: `2px solid ${color}`,
                                  background: `${color}1a`,
                                  boxSizing: "border-box",
                                }}
                              />
                            );
                          })}
                        </div>
                      ) : (
                        <VStack gap={3}>
                          {blocks.map((b, i) =>
                            !b.text ? (
                              <HStack key={i} gap={2} vAlign="center">
                                <Icon icon={PhotoIcon} size="sm" />
                                <Text type="supporting" color="secondary">
                                  {b.label ? `${b.label} détecté(e)` : "Élément détecté"}
                                </Text>
                              </HStack>
                            ) : b.label ? (
                              <VStack key={i} gap={1}>
                                {LABEL_BADGE[b.label] ? (
                                  <Badge label={b.label} variant={LABEL_BADGE[b.label]} />
                                ) : null}
                                <Text>{b.text}</Text>
                              </VStack>
                            ) : (
                              <Text key={i}>{b.text}</Text>
                            ),
                          )}
                        </VStack>
                      )}
                    </VStack>
                  </Card>
                ) : (
                  <EmptyState
                    icon={<Icon icon={DocumentMagnifyingGlassIcon} size="lg" />}
                    title="Le résultat s'affichera ici"
                    description="Choisis une image à gauche puis lance l'extraction."
                    isCompact
                  />
                )}
              </StackItem>
            </HStack>
          )}
        </LayoutContent>
      }
    />
  );
}
