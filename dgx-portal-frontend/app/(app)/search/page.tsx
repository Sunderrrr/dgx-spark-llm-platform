"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import { getJSON } from "@/lib/api";
import { useT, useLocale } from "@/lib/i18n";

type HfModel = {
  modelId?: string;
  pipeline_tag?: string;
  downloads?: number;
  tags?: string[];
  engine?: string;
  /** Présent avec `full=true` : un dépôt gated exige un jeton HF et fait
   * échouer le téléchargement — autant le voir dans les résultats. */
  gated?: boolean | string;
};

/** Une seule tâche à la fois, et « toutes » par défaut : la recherche couvre
 * tout Hugging Face. Filtrer par tâche était le défaut, et c'est ce qui rendait
 * introuvable un modèle taggé seulement `image-text-to-text` (cf. page « Ornith-1.5 »). */
const TASKS = [
  { value: "text-generation", label: "Génération de texte" },
  { value: "text2text-generation", label: "Texte vers texte" },
  { value: "conversational", label: "Conversation" },
  { value: "feature-extraction", label: "Plongements (embeddings)" },
  { value: "text-to-image", label: "Texte vers image" },
  { value: "text-to-video", label: "Texte vers vidéo" },
  { value: "image-to-text", label: "Image vers texte" },
];

export default function SearchPage() {
  const t = useT();
  // Une seule définition de la locale dans le dépôt (`useLocale`) : « 29 386 » en
  // français, « 29,386 » en anglais, sans y repenser sur chaque écran.
  const numLocale = useLocale();
  const [query, setQuery] = useState("");
  // Tâche vide = aucune restriction : c'est le défaut.
  const [task, setTask] = useState("");
  // Le tag GB10 ne marque qu'une poignée de modèles testés sur DGX Spark : il
  // était appliqué d'office, donc tout le reste de Hugging Face était invisible
  // sans que rien ne le dise. C'est un filtre utile, mais qu'on DEMANDE.
  const [gb10Only, setGb10Only] = useState(false);
  const [results, setResults] = useState<HfModel[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  // « HF n'a pas répondu » et « aucun modèle ne correspond » sont deux situations
  // différentes : les confondre faisait conclure à l'utilisateur que son modèle
  // n'existe pas. Le serveur distingue désormais les deux (502 vs 200 vide).
  const [error, setError] = useState<string | null>(null);
  // Le filtre GB10 ne donne rien, mais HF en connaît hors filtre : on le dit.
  const [horsGb10, setHorsGb10] = useState<boolean | null>(null);
  // Un jeton HF est-il configuré côté serveur ? Sa valeur ne quitte jamais le
  // serveur : l'interface ne sait que « il y en a un / il n'y en a pas ».
  const [hfToken, setHfToken] = useState<boolean | null>(null);
  // `has_more` vient de l'en-tête `Link` de Hugging Face, donc il est exact —
  // l'ancienne heuristique (« la page est pleine ») affichait un bouton qui
  // pouvait ne rien donner et en cachait un quand la dernière page était pleine.
  const [hasMore, setHasMore] = useState(false);
  // Chaque recherche porte un numéro : une réponse tardive à une frappe
  // précédente ne doit pas écraser l'affichage de la frappe en cours (le
  // debounce de 400 ms ne protège pas des réponses hors séquence).
  const seq = useRef(0);

  const runSearch = useCallback(async (q: string, tk: string, gb10: boolean, skip = 0) => {
    const mien = ++seq.current;
    const setBusy = skip > 0 ? setIsLoadingMore : setIsLoading;
    setBusy(true);
    try {
      const params = new URLSearchParams({ q, task: tk, gb10: gb10 ? "1" : "", skip: String(skip) });
      const data = await getJSON<{
        results: HfModel[]; page_size: number; has_more?: boolean;
        hors_gb10?: boolean | null; hf_token?: boolean;
      }>(`/api/search?${params}`);
      if (mien !== seq.current) return; // réponse périmée : on la jette
      setResults((prev) => (skip > 0 ? [...(prev ?? []), ...data.results] : data.results));
      setHasMore(Boolean(data.has_more));
      setHorsGb10(data.hors_gb10 ?? null);
      setHfToken(typeof data.hf_token === "boolean" ? data.hf_token : null);
      setError(null);
    } catch (e) {
      if (mien !== seq.current) return;
      // On dit ce qui s'est passé, et on garde ce qui était affiché : vider la
      // grille ferait croire à une recherche sans résultat.
      // Un code connu (panne de Hugging Face) a sa phrase traduite : sinon un
      // lecteur anglophone ne lit que le message français du serveur. Le détail
      // technique (nom de l'exception) reste dans les journaux du portail.
      const err = e as Error & { code?: string };
      if (err.code === "hf_indisponible") {
        setError(t("Hugging Face ne répond pas. Réessaie dans un instant."));
      } else {
        setError(e instanceof Error && e.message ? e.message : t("Recherche impossible."));
      }
      setHorsGb10(null);
    } finally {
      if (mien === seq.current) setBusy(false);
    }
  }, [t]);

  // Automatically searches as soon as the query, task or gb10 filter
  // changes — 400ms debounce on typing so we don't hammer the HF API on
  // every character. Before this fix, only an explicit click on "Search"
  // triggered a new search: typing text had no visible effect, giving the
  // impression of frozen/wrong results.
  useEffect(() => {
    const id = setTimeout(() => runSearch(query, task, gb10Only, 0), 400);
    return () => clearTimeout(id);
  }, [query, task, gb10Only, runSearch]);

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5}>
            <VStack gap={1}>
              <Heading level={1}>{t("Chercher un modèle")}</Heading>
              <Text type="supporting" color="secondary">{t("Recherche en direct dans tout Hugging Face. Demande ensuite le lancement du modèle sur le DGX.")}</Text>
            </VStack>

            <HStack gap={2} wrap="wrap" vAlign="end">
              <TextInput
                label={t("Recherche")}
                isLabelHidden
                value={query}
                onChange={setQuery}
                placeholder={t("Nom de modèle, ex: Qwen, Ornith, Mistral...")}
                startIcon={MagnifyingGlassIcon}
              />
              <Selector
                label={t("Tâche")}
                isLabelHidden
                value={task || null}
                onChange={(v) => setTask(v ?? "")}
                placeholder={t("Toutes les tâches")}
                hasClear
                hasSearch
                options={TASKS.map((x) => ({ value: x.value, label: t(x.label) }))}
              />
              <Button
                label={t("Chercher")}
                variant="primary"
                icon={<Icon icon={MagnifyingGlassIcon} size="sm" />}
                onClick={() => runSearch(query, task, gb10Only)}
              />
              <Switch
                label={t("Seulement testés sur DGX Spark (GB10)")}
                value={gb10Only}
                onChange={setGb10Only}
              />
            </HStack>

            <Text type="supporting" color="secondary">
              {gb10Only
                ? t("Filtre actif : seuls les modèles portant le tag GB10 — testés sur DGX Spark — sont affichés.")
                : t("Toutes les tâches, tout le catalogue. Le tag GB10 est signalé par un badge quand le modèle le porte.")}
            </Text>

            {hfToken === false && (
              <Text type="supporting" color="secondary">
                {t("Sans jeton Hugging Face, les dépôts à accès restreint n'apparaissent pas dans les résultats.")}
              </Text>
            )}

            {error && (
              <Card>
                <HStack gap={2} vAlign="center" hAlign="between">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={ExclamationTriangleIcon} size="sm" />
                    <Text color="secondary">{error}</Text>
                  </HStack>
                  <Button
                    label={t("Réessayer")}
                    variant="secondary"
                    size="sm"
                    onClick={() => runSearch(query, task, gb10Only)}
                  />
                </HStack>
              </Card>
            )}

            {!results || (results.length === 0 && !isLoading) ? (
              <EmptyState
                icon={<Icon icon={MagnifyingGlassIcon} size="lg" />}
                title={
                  error
                    ? t("Résultats indisponibles.")
                    : query
                      ? `${t("Aucun résultat pour")} « ${query} ».`
                      : t("Tape un nom de modèle pour explorer Hugging Face.")
                }
                description={
                  // « Ce modèle n'existe pas » et « il est mal orthographié » se
                  // ressemblent : la recherche de Hugging Face porte sur le NOM du
                  // dépôt (pas sur sa description), donc « orith1.5 » ne trouve pas
                  // « Ornith-1.5 ». Le dire, et donner le lien qui permet de
                  // vérifier soi-même plutôt que de conclure à une panne.
                  horsGb10 === true
                    ? t("Aucun modèle GB10 ne correspond, mais il y en a sur tout Hugging Face : décoche le filtre ci-dessus.")
                    : horsGb10 === false
                      ? t("Hugging Face ne connaît aucun modèle pour cette recherche.")
                      : query
                        ? t("La recherche porte sur le nom du dépôt, pas sur sa description : vérifie l'orthographe.")
                        : undefined
                }
                actions={
                  query ? (
                    <Button
                      label={t("Chercher sur Hugging Face")}
                      variant="secondary"
                      icon={<Icon icon={ArrowTopRightOnSquareIcon} size="sm" />}
                      onClick={() =>
                        window.open(`https://huggingface.co/models?search=${encodeURIComponent(query)}`, "_blank")
                      }
                    />
                  ) : undefined
                }
              />
            ) : (
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {`${results.length.toLocaleString(numLocale)} ${t("modèles affichés")}`}
                  {query ? ` · ${t("résultats pour")} « ${query} »` : ` · ${t("les plus téléchargés de Hugging Face")}`}
                </Text>
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
                            {model.tags?.includes("gb10") && (
                              <Badge label="GB10" variant="green" />
                            )}
                            {model.gated && (
                              <Badge
                                label={t("Accès restreint")}
                                variant="orange"
                                icon={<Icon icon={ExclamationTriangleIcon} size="sm" />}
                              />
                            )}
                          </HStack>
                          <Text type="supporting" color="secondary">
                            {(model.downloads ?? 0).toLocaleString(numLocale)} DL
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
                            label={t("Demander")}
                            variant="secondary"
                            size="sm"
                            icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                            href={`/request?model=${encodeURIComponent(model.modelId || "")}`}
                          />
                        </HStack>
                      </VStack>
                    </Card>
                  ))}
                </Grid>
              </VStack>
            )}

            {hasMore && (
              <Button
                label={t("Charger plus")}
                variant="secondary"
                icon={<Icon icon={MagnifyingGlassIcon} size="sm" />}
                isLoading={isLoadingMore}
                onClick={() => runSearch(query, task, gb10Only, results?.length ?? 0)}
              />
            )}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
