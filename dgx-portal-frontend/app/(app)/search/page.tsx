"use client";

import { useCallback, useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Switch } from "@astryxdesign/core/Switch";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import {
  MagnifyingGlassIcon,
  ArrowTopRightOnSquareIcon,
  PaperAirplaneIcon,
  CpuChipIcon,
  BoltIcon,
} from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";

type HfModel = {
  modelId?: string;
  pipeline_tag?: string;
  downloads?: number;
  tags?: string[];
  engine?: string;
};

const TASKS = [
  { value: "text-generation", label: "Text Generation" },
  { value: "text2text-generation", label: "Text2Text" },
  { value: "conversational", label: "Conversational" },
  { value: "feature-extraction", label: "Embeddings" },
];

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("text-generation");
  const [gb10Only, setGb10Only] = useState(true);
  const [results, setResults] = useState<HfModel[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const runSearch = useCallback(async (q: string, t: string, gb10: boolean) => {
    if (!q && !gb10) {
      setResults([]);
      return;
    }
    setIsLoading(true);
    try {
      const params = new URLSearchParams({ q, task: t, all: gb10 ? "" : "1" });
      const data = await getJSON<{ results: HfModel[] }>(`/api/search?${params}`);
      setResults(data.results);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(() => runSearch(query, task, gb10Only), 0);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gb10Only]);

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5}>
            <VStack gap={1}>
              <Heading level={1}>Chercher un modèle</Heading>
              <Text type="supporting" color="secondary">
                Explore le catalogue Hugging Face et demande le lancement d&apos;un modèle sur le DGX.
              </Text>
            </VStack>

            <HStack gap={2} wrap="wrap" vAlign="end">
              <TextInput
                label="Recherche"
                isLabelHidden
                value={query}
                onChange={setQuery}
                placeholder="Nom de modèle, ex: Qwen, Llama, Mistral..."
                startIcon={MagnifyingGlassIcon}
              />
              <Selector
                label="Tâche"
                isLabelHidden
                value={task}
                onChange={(v) => {
                  const next = v ?? "text-generation";
                  setTask(next);
                  runSearch(query, next, gb10Only);
                }}
                options={TASKS}
              />
              <Button
                label="Chercher"
                variant="primary"
                icon={<Icon icon={MagnifyingGlassIcon} size="sm" />}
                onClick={() => runSearch(query, task, gb10Only)}
              />
              <Switch
                label="Tout Hugging Face"
                value={!gb10Only}
                onChange={(checked) => setGb10Only(!checked)}
              />
            </HStack>

            <Text type="supporting" color="secondary">
              {gb10Only
                ? "Seuls les modèles testés sur DGX Spark / GB10 sont affichés. Décoche pour élargir à tout Hugging Face."
                : "Recherche élargie à tout Hugging Face — ces modèles ne sont pas garantis de tourner sur le GB10."}
            </Text>

            {!results || (results.length === 0 && !isLoading) ? (
              <EmptyState
                icon={<Icon icon={MagnifyingGlassIcon} size="lg" />}
                title={query ? `Aucun résultat pour « ${query} ».` : "Tape un nom de modèle pour explorer Hugging Face."}
              />
            ) : (
              <Grid columns={{ minWidth: 280, max: 3 }} gap={3}>
                {results.map((model) => (
                  <Card key={model.modelId}>
                    <VStack gap={2}>
                      <HStack hAlign="between" vAlign="start">
                        <HStack gap={1}>
                          <Badge label={model.pipeline_tag || "—"} variant="neutral" />
                          <Badge
                            label={model.engine === "llamacpp" ? "llama.cpp" : "vLLM"}
                            variant={model.engine === "llamacpp" ? "purple" : "blue"}
                            icon={<Icon icon={model.engine === "llamacpp" ? CpuChipIcon : BoltIcon} size="sm" />}
                          />
                        </HStack>
                        <Text type="supporting" color="secondary">
                          {(model.downloads ?? 0).toLocaleString("fr-FR")} DL
                        </Text>
                      </HStack>
                      <Text weight="semibold" wordBreak="break-all">
                        {model.modelId}
                      </Text>
                      {model.tags && model.tags.length > 0 && (
                        <HStack gap={1} wrap="wrap">
                          {model.tags.slice(0, 4).map((tag) => (
                            <Badge key={tag} label={tag} variant="neutral" />
                          ))}
                        </HStack>
                      )}
                      <HStack gap={2}>
                        <Button
                          label="HF"
                          variant="secondary"
                          size="sm"
                          icon={<Icon icon={ArrowTopRightOnSquareIcon} size="sm" />}
                          onClick={() => window.open(`https://huggingface.co/${model.modelId}`, "_blank")}
                        />
                        <Button
                          label="Demander"
                          variant="secondary"
                          size="sm"
                          icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                          onClick={() =>
                            (window.location.href = `/request?model=${encodeURIComponent(model.modelId || "")}`)
                          }
                        />
                      </HStack>
                    </VStack>
                  </Card>
                ))}
              </Grid>
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
