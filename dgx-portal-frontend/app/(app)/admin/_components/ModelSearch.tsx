"use client";

/**
 * Hugging Face search for the admin catalog.
 *
 * The operator's rule (2026-11-17): the list must be EVERY model on Hugging
 * Face, filtered by the category being edited — never a static list. Selecting
 * one pre-fills the catalog form (name, engine, engine arguments) from what the
 * repository actually contains: a GGUF header gives the trained context, a
 * transformers checkpoint gives `max_position_embeddings`, and the file sizes
 * give the disk it will cost.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { List, ListItem } from "@astryxdesign/core/List";
import { Banner } from "@astryxdesign/core/Banner";
import { getJSON, postFormJSON } from "@/lib/api";
import { useCsrf } from "@/lib/useCsrf";
import { useT, useLocale } from "@/lib/i18n";

/// One search hit, as `/api/search` returns it.
export type HfHit = {
  id?: string;
  modelId?: string;
  downloads?: number;
  likes?: number;
  gated?: boolean | string;
  engine?: string;
  tags?: string[];
};

/// What `/admin/model/preset` answers.
export type HfPreset = {
  ok?: boolean;
  hf_model_id?: string;
  name?: string;
  engine?: string;
  vllm_args?: string;
  context?: number;
  size_gb?: number;
  gated?: boolean | string;
  gguf?: boolean;
  context_source?: string;
  error?: string;
};

type SearchAnswer = { ok?: boolean; results?: HfHit[]; error?: string };

const DEBOUNCE_MS = 350;

export function HfModelSearch({
  task,
  isDisabled,
  onPick,
}: {
  /// The Hugging Face task of the category being edited (`text-generation`…).
  task: string;
  isDisabled: boolean;
  onPick: (preset: HfPreset) => void;
}) {
  const t = useT();
  const locale = useLocale();
  const csrf = useCsrf();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<HfHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const seq = useRef(0);

  // Debounced live search: every keystroke would cost a Hugging Face call.
  useEffect(() => {
    const query = q.trim();
    const mine = ++seq.current;
    // The short-query reset happens INSIDE the timer too: a synchronous setState
    // in an effect body is what `react-hooks/set-state-in-effect` forbids.
    const timer = setTimeout(async () => {
      if (query.length < 2) {
        setHits([]);
        setError(null);
        setBusy(false);
        return;
      }
      setBusy(true);
      try {
        const d = await getJSON<SearchAnswer>(
          `/api/search?q=${encodeURIComponent(query)}&task=${encodeURIComponent(task)}&skip=0`,
        );
        if (mine !== seq.current) return; // a newer query won
        setHits(d.results ?? []);
        setError(d.ok === false ? (d.error ?? t("Recherche indisponible.")) : null);
      } catch {
        if (mine === seq.current) setError(t("Recherche indisponible."));
      } finally {
        if (mine === seq.current) setBusy(false);
      }
    }, query.length < 2 ? 0 : DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q, task, t]);

  const pick = useCallback(
    async (repo: string) => {
      setPicked(repo);
      const d = await postFormJSON<HfPreset>("/admin/model/preset", csrf, { hf_model_id: repo });
      if (d.ok) {
        onPick(d);
        setError(null);
      } else {
        setError(d.error ?? t("Préréglage indisponible."));
      }
    },
    [csrf, onPick, t],
  );

  return (
    <VStack gap={2}>
      <TextInput
        label={t("Rechercher sur Hugging Face")}
        description={t("Tout le catalogue Hugging Face, filtré sur cette catégorie. Sélectionne un modèle : nom, moteur et paramètres sont pré-remplis.")}
        value={q}
        onChange={setQ}
        placeholder={t("ex. {v}").replace("{v}", "qwen3-8b")}
        size="sm"
        isDisabled={isDisabled}
      />
      {busy ? <Text type="supporting" color="secondary">{t("Recherche…")}</Text> : null}
      {error ? <Banner status="error" title={error} container="card" /> : null}
      {hits.length ? (
        // Rows, not cards: a result list is dense data (AGENTS.md).
        <List>
          {hits.slice(0, 6).map((h) => {
            const repo = h.id ?? h.modelId ?? "";
            if (!repo) return null;
            return (
              <ListItem
                key={repo}
                label={repo}
                description={
                  (h.engine === "llamacpp" ? "GGUF · llama.cpp" : "transformers · vLLM") +
                  (typeof h.downloads === "number"
                    ? ` · ${h.downloads.toLocaleString(locale)} ${t("téléchargements")}`
                    : "")
                }
                onClick={() => void pick(repo)}
                endContent={
                  <HStack gap={2} vAlign="center">
                    {h.gated ? <Badge label="gated" variant="warning" /> : null}
                    <Button
                      label={picked === repo ? t("Sélectionné") : t("Utiliser")}
                      variant="secondary"
                      size="sm"
                      isDisabled={isDisabled || picked === repo}
                      onClick={() => void pick(repo)}
                    />
                  </HStack>
                }
              />
            );
          })}
        </List>
      ) : null}
    </VStack>
  );
}

/// The one-line summary shown once a hit is picked.
export function PresetSummary({ preset }: { preset: HfPreset }) {
  const t = useT();
  const parts = [
    preset.engine === "llamacpp" ? "llama.cpp" : "vLLM",
    preset.context ? t("contexte {v}").replace("{v}", String(preset.context)) : "",
    typeof preset.size_gb === "number" && preset.size_gb > 0
      ? t("{v} Go à télécharger").replace("{v}", String(preset.size_gb))
      : "",
  ].filter(Boolean);
  return (
    <Text type="supporting" color="secondary">
      {parts.join(" · ")}
      {preset.gated ? ` · ${t("accès sur autorisation Hugging Face")}` : ""}
    </Text>
  );
}
