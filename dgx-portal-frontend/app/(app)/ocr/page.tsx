"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { FileInput } from "@astryxdesign/core/FileInput";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Markdown } from "@astryxdesign/core/Markdown";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { DocumentTextIcon, DocumentMagnifyingGlassIcon, MoonIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { streamOcr } from "@/lib/api";

type HistoryItem = { id: number; text: string; created_at: string };
type RunningModel = { name: string; kind: "chat" | "ocr" | "video"; exposed: boolean };

export default function OcrPage() {
  const csrf = useCsrf();
  const showToast = useToast();
  const [image, setImage] = useState<File | null>(null);
  const [instruction, setInstruction] = useState("document parsing.");
  const [isLoading, setIsLoading] = useState(false);
  const [text, setText] = useState("");
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [available, setAvailable] = useState<boolean | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
      .then((d) => setAvailable(!!d?.running_models?.some((m: RunningModel) => m.kind === "ocr")))
      .catch(() => setAvailable(null));
  }, []);

  async function extract() {
    if (!image || !csrf) return;
    setIsLoading(true);
    setText("");
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
                      <TextInput
                        label="Instruction (optionnel)"
                        value={instruction}
                        onChange={setInstruction}
                        placeholder="document parsing."
                        isDisabled={isLoading}
                      />
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
                            label={h.text.slice(0, 80) || "(vide)"}
                            labelLines={1}
                            description={new Date(h.created_at).toLocaleString("fr-FR")}
                            startContent={<DocumentTextIcon width={20} height={20} />}
                            onClick={() => setText(h.text)}
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
                    <Markdown isStreaming={isLoading}>{text}</Markdown>
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
