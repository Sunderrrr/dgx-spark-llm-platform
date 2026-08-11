"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Markdown } from "@astryxdesign/core/Markdown";
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
import { useT } from "@/lib/i18n";

type HistoryItem = { id: number; text: string; created_at: string; has_image: boolean };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };
type OcrBlock = { label: string; coords: [number, number, number, number] | null; text: string };

// Two models, two totally different output formats for the detected zones —
// the format is auto-detected from the received text (the actually-running
// model can change via the admin OCR catalog):
//
// - baidu/Unlimited-OCR: lines "<label> [x1, y1, x2, y2]text", commas,
//   coordinates normalized 0-1000.
// - datalab-to/chandra-ocr-2: HTML with blocks
//   <div data-label="..." data-bbox="x0 y0 x1 y1">...</div>, bbox separated by
//   SPACES (not commas), same 0-1000 normalization. We extract the
//   textContent (never innerHTML): the result is therefore never injected
//   as HTML into the page, just text, even if the model produces
//   malformed or deliberately booby-trapped HTML (image containing "<script>…" text).
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

// ── chandra HTML → Markdown ───────────────────────────────────────────────
// chandra-ocr ALWAYS outputs HTML (even when asked for Markdown: it's
// fine-tuned for this format). This HTML is clean and limited to a known set
// of tags (h1-h5, table, ul/ol, p, b/i, math…), which we convert here to GFM
// Markdown — real tables included — for readable display via <Markdown>.
// We NEVER render the model's HTML directly (a booby-trapped image could
// contain <script>…): we only read tagName/textContent, never innerHTML.
function tableToMarkdown(table: Element): string {
  const rows = Array.from(table.querySelectorAll("tr"));
  if (!rows.length) return "";
  const cellsOf = (tr: Element) =>
    Array.from(tr.children)
      .filter((c) => c.tagName === "TD" || c.tagName === "TH")
      .map((c) => (c.textContent || "").trim().replace(/\|/g, "\\|").replace(/\s*\n\s*/g, " "));
  const nCols = Math.max(...rows.map((r) => cellsOf(r).length), 1);
  const pad = (arr: string[]) => { const a = [...arr]; while (a.length < nCols) a.push(""); return a; };
  const lines = [
    "| " + pad(cellsOf(rows[0])).join(" | ") + " |",
    "| " + Array(nCols).fill("---").join(" | ") + " |",
    ...rows.slice(1).map((r) => "| " + pad(cellsOf(r)).join(" | ") + " |"),
  ];
  return lines.join("\n");
}

function nodeToMarkdown(node: Node): string {
  if (node.nodeType === 3 /* text */) return node.textContent || "";
  if (node.nodeType !== 1 /* element */) return "";
  const el = node as Element;
  const inner = () => Array.from(el.childNodes).map(nodeToMarkdown).join("");
  switch (el.tagName.toLowerCase()) {
    case "h1": return `\n\n# ${inner().trim()}\n\n`;
    case "h2": return `\n\n## ${inner().trim()}\n\n`;
    case "h3": return `\n\n### ${inner().trim()}\n\n`;
    case "h4": return `\n\n#### ${inner().trim()}\n\n`;
    case "h5": case "h6": return `\n\n##### ${inner().trim()}\n\n`;
    case "p": case "div": return `\n\n${inner().trim()}\n\n`;
    case "br": return "  \n";
    case "hr": return "\n\n---\n\n";
    case "b": case "strong": return `**${inner().trim()}**`;
    case "i": case "em": return `*${inner().trim()}*`;
    case "del": return `~~${inner().trim()}~~`;
    case "sup": return `^${inner().trim()}`;
    case "sub": return `~${inner().trim()}`;
    case "code": return `\`${el.textContent || ""}\``;
    case "pre": return `\n\n\`\`\`\n${el.textContent || ""}\n\`\`\`\n\n`;
    case "math": return `$${(el.textContent || "").trim()}$`;
    case "chem": return `\`${(el.textContent || "").trim()}\``;
    case "a": { const href = el.getAttribute("href"); return href ? `[${inner().trim()}](${href})` : inner(); }
    case "img": { const alt = (el.getAttribute("alt") || el.textContent || "").trim(); return alt ? `\n\n*[Image : ${alt}]*\n\n` : ""; }
    case "ul": return "\n\n" + Array.from(el.children).filter((c) => c.tagName === "LI").map((li) => `- ${nodeToMarkdown(li).trim()}`).join("\n") + "\n\n";
    case "ol": return "\n\n" + Array.from(el.children).filter((c) => c.tagName === "LI").map((li, i) => `${i + 1}. ${nodeToMarkdown(li).trim()}`).join("\n") + "\n\n";
    case "table": return `\n\n${tableToMarkdown(el)}\n\n`;
    default: return inner();
  }
}

/** Renders the OCR result as Markdown: converted chandra HTML (tables included),
 *  or plain text for line-by-line models (Unlimited-OCR). */
function ocrToMarkdown(raw: string, blocks: OcrBlock[]): string {
  if (!raw) return "";
  if (raw.includes("data-bbox=")) {
    const doc = new DOMParser().parseFromString(raw, "text/html");
    return Array.from(doc.body.childNodes).map(nodeToMarkdown).join("").replace(/\n{3,}/g, "\n\n").trim();
  }
  return blocks.map((b) => b.text).filter(Boolean).join("\n\n");
}

/** Short preview for the history: readable text, never the raw HTML/coordinates. */
function ocrPreviewText(raw: string): string {
  return parseOcrBlocks(raw)
    .map((b) => b.text)
    .filter(Boolean)
    .join(" ")
    .slice(0, 80);
}

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

// True on phone-width viewports. Used to stack the two-column layout vertically
// so the result panel (image + text) isn't squeezed off-screen on mobile.
function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 820px)");
    const update = () => setNarrow(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return narrow;
}

export default function OcrPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const isNarrow = useIsNarrow();
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
  // resultImageUrl (shown in the right panel) is deliberately independent of
  // previewUrl (upload widget on the left): otherwise picking a new image
  // while an old result is displayed would revoke the blob that the "detected
  // zones" view still needs.
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
  const markdown = useMemo(() => ocrToMarkdown(text, blocks), [text, blocks]);

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
      showToast({ body: t("OCR injoignable."), type: "error" });
    } finally {
      setIsLoading(false);
    }
  }

  function copyResult() {
    // We copy the Markdown (readable, tables included), not the model's raw HTML.
    navigator.clipboard.writeText(markdown || text).then(() => showToast({ body: t("Copié."), type: "info" }));
  }

  // Side-by-side on desktop, stacked on mobile.
  const Split = isNarrow ? VStack : HStack;
  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable={available === false || isNarrow}>
          {available === false ? (
            <EmptyState
              icon={<Icon icon={MoonIcon} size="lg" />}
              title={t("Aucun modèle OCR n'est disponible")}
              description={t("Demande à un admin de démarrer un modèle OCR pour utiliser cette page.")}
            />
          ) : (
            <Split gap={5} height={isNarrow ? undefined : "100%"}>
              {/* On desktop each column scrolls on its own (the left one holds
                  the history, up to 20 entries). On mobile we stack them and let
                  the page scroll, so the result panel isn't squeezed off-screen. */}
              <StackItem isScrollable={!isNarrow}>
                <VStack gap={5} width={isNarrow ? "100%" : 360}>
                  <VStack gap={1}>
                    <Heading level={1}>OCR</Heading>
                    <Text type="supporting" color="secondary">
                      {t("Extrait le texte d'une image ou d'un document scanné.")}
                    </Text>
                  </VStack>

                  <Card>
                    <VStack gap={4}>
                      {previewUrl ? (
                        <VStack gap={2}>
                          <AspectRatio ratio={4 / 3} fit="cover">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={previewUrl} alt={t("Aperçu du document")} />
                          </AspectRatio>
                          <Button
                            label={t("Changer d'image")}
                            variant="ghost"
                            size="sm"
                            icon={<Icon icon={XMarkIcon} size="sm" />}
                            isDisabled={isLoading}
                            onClick={() => setImage(null)}
                          />
                        </VStack>
                      ) : (
                        <FileInput
                          label={t("Image ou scan")}
                          value={image}
                          onChange={(f) => setImage(f as File | null)}
                          accept="image/png,image/jpeg,image/webp"
                          maxSize={15 * 1024 * 1024}
                          mode="dropzone"
                          description={t("PNG, JPEG ou WebP — 15 Mo max.")}
                          isDisabled={isLoading}
                          isRequired
                        />
                      )}
                      {!isChandra && (
                        <TextInput
                          label={t("Instruction (optionnel)")}
                          value={instruction}
                          onChange={setInstruction}
                          placeholder="document parsing."
                          isDisabled={isLoading}
                        />
                      )}
                      <Button
                        label={t("Extraire le texte")}
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
                        {t("Historique")} ({history.length} {history.length > 1 ? t("derniers") : t("dernier")})
                      </Text>
                      <VStack gap={0}>
                        {history.map((h) => (
                          <Item
                            key={h.id}
                            label={ocrPreviewText(h.text) || t("(vide)")}
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

              <StackItem size={isNarrow ? undefined : "fill"} isScrollable={!isNarrow}>
                {text ? (
                  <Card>
                    <VStack gap={3}>
                      <HStack hAlign="between" vAlign="center">
                        <StatusDot
                          variant={isLoading ? "accent" : "success"}
                          label={isLoading ? t("Extraction en cours…") : t("Terminé")}
                        />
                        <HStack gap={2} vAlign="center">
                          {resultImageUrl && boxedBlocks.length > 0 && (
                            <SegmentedControl label={t("Vue du résultat")} value={resultView} onChange={(v) => setResultView(v as "text" | "boxes")}>
                              <SegmentedControlItem value="text" label={t("Texte")} icon={<Icon icon={DocumentTextIcon} size="sm" />} />
                              <SegmentedControlItem value="boxes" label={t("Zones détectées")} icon={<Icon icon={ViewfinderCircleIcon} size="sm" />} />
                            </SegmentedControl>
                          )}
                          <Button
                            label={t("Copier")}
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
                          <img src={resultImageUrl} alt={t("Zones détectées")} style={{ width: "100%", height: "auto", display: "block", borderRadius: "var(--radius-md)" }} />
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
                      ) : markdown ? (
                        // Result rendered as Markdown: headings, lists and above
                        // all the detected TABLES (converted to GFM tables). The
                        // component also handles the partial stream during extraction.
                        <Markdown isStreaming={isLoading}>{markdown}</Markdown>
                      ) : (
                        <HStack gap={2} vAlign="center">
                          <Icon icon={PhotoIcon} size="sm" />
                          <Text type="supporting" color="secondary">{t("Élément détecté")}</Text>
                        </HStack>
                      )}
                    </VStack>
                  </Card>
                ) : (
                  <EmptyState
                    icon={<Icon icon={DocumentMagnifyingGlassIcon} size="lg" />}
                    title={t("Le résultat s'affichera ici")}
                    description={t("Choisis une image à gauche puis lance l'extraction.")}
                    isCompact
                  />
                )}
              </StackItem>
            </Split>
          )}
        </LayoutContent>
      }
    />
  );
}
