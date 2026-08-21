"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Switch } from "@astryxdesign/core/Switch";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Item } from "@astryxdesign/core/Item";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { useToast } from "@astryxdesign/core/Toast";
import {
  SparklesIcon,
  TrashIcon,
  PlusIcon,
  ShieldCheckIcon,
  ArrowPathIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, sendJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

type MemNode = { id: number; name: string; kind: string; created_at: string };
type MemEdge = {
  id: number;
  relation: string;
  fact: string;
  source: string;
  created_at: string;
  src_id: number;
  dst_id: number | null;
  subject: string;
  object: string | null;
};
type MemGraph = { enabled: boolean; max_facts: number; nodes: MemNode[]; edges: MemEdge[] };

const EMPTY: MemGraph = { enabled: false, max_facts: 400, nodes: [], edges: [] };

export default function MemoryPage() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [graph, setGraph] = useState<MemGraph>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [subject, setSubject] = useState("");
  const [fact, setFact] = useState("");

  const load = useCallback(async () => {
    try {
      setGraph(await getJSON<MemGraph>("/api/memory"));
    } catch {
      showToast({ body: t("Chargement impossible."), type: "error" });
    } finally {
      setLoading(false);
    }
  }, [t, showToast]);

  // Chargement au montage : la mise à jour d'état a lieu APRÈS l'await, jamais
  // dans le corps de l'effet (même motif que les autres pages de données).
  useEffect(() => {
    getJSON<MemGraph>("/api/memory")
      .then(setGraph)
      .catch(() => showToast({ body: t("Chargement impossible."), type: "error" }))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- au montage uniquement
  }, []);

  // Les faits sont groupés PAR SUJET : c'est la forme du graphe, et c'est ce qui
  // rend lisible « ce que l'IA sait sur X » — une liste à plat ne le montrerait pas.
  const bySubject = useMemo(() => {
    const m = new Map<string, MemEdge[]>();
    for (const e of graph.edges) {
      const list = m.get(e.subject) ?? [];
      list.push(e);
      m.set(e.subject, list);
    }
    return [...m.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [graph.edges]);

  async function toggle(enabled: boolean) {
    const r = await sendJSON<{ ok: boolean; enabled: boolean }>("/api/memory/enabled", csrf, { enabled });
    setGraph((g) => ({ ...g, enabled: r.enabled }));
    showToast({
      body: r.enabled
        ? t("Mémoire activée.")
        : t("Mémoire désactivée. Ce qui est déjà mémorisé est conservé."),
      type: "info",
    });
  }

  async function addFact() {
    if (!subject.trim() || !fact.trim()) return;
    const r = await sendJSON<{ ok: boolean; error?: string }>("/api/memory/facts", csrf, {
      subject,
      fact,
    });
    if (!r.ok) {
      showToast({ body: r.error ?? t("Ajout impossible."), type: "error" });
      return;
    }
    setSubject("");
    setFact("");
    void load();
  }

  async function forget(id: number) {
    await sendJSON(`/api/memory/facts/${id}`, csrf, undefined, "DELETE");
    void load();
  }

  async function purge() {
    const r = await sendJSON<{ ok: boolean; deleted: number }>("/api/memory/purge", csrf);
    showToast({ body: `${t("Mémoire effacée")} — ${r.deleted} ${t("informations")}`, type: "info" });
    void load();
  }

  return (
    <Layout
      header={
        <VStack gap={1} padding={4}>
          <Heading level={1}>{t("Mémoire")}</Heading>
          <Text type="supporting" color="secondary">
            {t("Ce que l'assistant retient de toi, et que tu contrôles entièrement.")}
          </Text>
        </VStack>
      }
      content={
        <LayoutContent padding={4}>
          <VStack gap={4} maxWidth={900}>
            <Card padding={4}>
              <HStack hAlign="between" vAlign="center" gap={3}>
                <StackItem size="fill">
                  <VStack gap={1}>
                    <HStack gap={2} vAlign="center">
                      <Icon icon={ShieldCheckIcon} size="sm" color="secondary" />
                      <Text weight="semibold">{t("Activer la mémoire")}</Text>
                    </HStack>
                    <Text type="supporting" color="secondary">
                      {t(
                        "Désactivée par défaut. Une fois activée, l'assistant peut retenir des informations durables te concernant pour les réutiliser plus tard.",
                      )}
                    </Text>
                  </VStack>
                </StackItem>
                <Switch
                  label={t("Activer la mémoire")}
                  isLabelHidden
                  value={graph.enabled}
                  onChange={(v) => void toggle(v)}
                />
              </HStack>
            </Card>

            <Card padding={4}>
              <VStack gap={3}>
                <Text weight="semibold">{t("Ajouter une information")}</Text>
                <HStack gap={2} vAlign="end" wrap="wrap">
                  <StackItem size="fill">
                    <TextInput
                      label={t("Sujet")}
                      value={subject}
                      onChange={setSubject}
                      placeholder={t("ex : vLLM")}
                    />
                  </StackItem>
                  <StackItem size="fill">
                    <TextInput
                      label={t("Information")}
                      value={fact}
                      onChange={setFact}
                      placeholder={t("ex : Je sers mes modèles avec vLLM 0.27.")}
                    />
                  </StackItem>
                  <Button
                    label={t("Ajouter")}
                    variant="primary"
                    icon={<Icon icon={PlusIcon} size="sm" />}
                    isDisabled={!subject.trim() || !fact.trim()}
                    onClick={() => void addFact()}
                  />
                </HStack>
              </VStack>
            </Card>

            <Card padding={4}>
              <VStack gap={3}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{t("Ce qui est mémorisé")}</Text>
                    <Badge label={String(graph.edges.length)} variant="neutral" />
                  </HStack>
                  <HStack gap={2}>
                    <Button
                      label={t("Rafraîchir")}
                      variant="ghost"
                      size="sm"
                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                      onClick={() => void load()}
                    />
                    {graph.edges.length > 0 && (
                      <Button
                        label={t("Tout effacer")}
                        variant="destructive"
                        size="sm"
                        icon={<Icon icon={TrashIcon} size="sm" />}
                        onClick={() => void purge()}
                      />
                    )}
                  </HStack>
                </HStack>

                {/* Le plafond est visible : quand il approche, l'assistant ne peut
                    plus rien mémoriser et il faut faire du tri soi-même. */}
                <VStack gap={1}>
                  <ProgressBar
                    label={t("Capacité utilisée")}
                    isLabelHidden
                    value={graph.edges.length}
                    max={graph.max_facts}
                  />
                  <Text type="supporting" color="secondary">
                    {graph.edges.length} / {graph.max_facts} {t("informations")}
                  </Text>
                </VStack>

                {loading ? (
                  <Text color="secondary">{t("Chargement…")}</Text>
                ) : graph.edges.length === 0 ? (
                  <EmptyState
                    icon={<Icon icon={SparklesIcon} size="lg" color="secondary" />}
                    title={t("Rien de mémorisé pour l'instant")}
                    description={
                      graph.enabled
                        ? t("Les informations apparaîtront ici au fil des conversations.")
                        : t("Active la mémoire ci-dessus, ou ajoute une information à la main.")
                    }
                  />
                ) : (
                  bySubject.map(([subj, edges]) => (
                    <Collapsible key={subj} trigger={`${subj} (${edges.length})`} defaultIsOpen>
                      <VStack gap={0}>
                        {edges.map((e) => (
                          <Item
                            key={e.id}
                            label={e.fact}
                            description={
                              // La relation générique (ajout manuel sans relation
                              // précisée) n'apprend rien au lecteur : on ne montre
                              // alors que l'origine du fait.
                              e.object
                                ? `${e.relation} → ${e.object}`
                                : [
                                    e.relation === "à propos de" ? null : e.relation,
                                    e.source === "user" ? t("ajouté par toi") : t("appris en conversation"),
                                  ]
                                    .filter(Boolean)
                                    .join(" · ")
                            }
                            endContent={
                              <Button
                                label={t("Oublier")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={TrashIcon} size="sm" />}
                                onClick={() => void forget(e.id)}
                              />
                            }
                          />
                        ))}
                      </VStack>
                    </Collapsible>
                  ))
                )}
              </VStack>
            </Card>

            <Text type="supporting" color="secondary">
              {t(
                "Personne d'autre ne peut lire ta mémoire, administrateurs compris. Désactiver la collecte n'efface rien : utilise « Tout effacer » pour ça.",
              )}
            </Text>
          </VStack>
        </LayoutContent>
      }
    />
  );
}
