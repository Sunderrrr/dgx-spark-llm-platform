"use client";

import { useCallback, useMemo, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Item } from "@astryxdesign/core/Item";
import { Icon } from "@astryxdesign/core/Icon";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { useToast } from "@astryxdesign/core/Toast";
import { useSeed, type Seed } from "@/lib/seed";
import {
  AdjustmentsHorizontalIcon,
  ArrowPathIcon,
  PlayIcon,
  PlusIcon,
  StopIcon,
  TrashIcon,
  ScaleIcon,
} from "@heroicons/react/24/outline";
import { getJSON, sendJSON } from "@/lib/api";
import { useCsrf } from "@/lib/useCsrf";
import { tServer, useT } from "@/lib/i18n";
import {
  answerHeadline,
  answerOptions,
  buildDecisionRequest,
  formatProbability,
  MAX_QUESTIONS,
  MAX_STATE_BYTES,
  type DecisionAnswer,
  type QuestionDraft,
  type QuestionType,
} from "@/lib/decisions";

type DecisionsStatus = {
  model: string;
  enabled: boolean;
  status: string;
  port: number | null;
  libre_gib: number | null;
  max_state_bytes: number;
  max_questions: number;
  admin?: boolean;
};

type RunResult = { ok: true; model: string; answers: Record<string, DecisionAnswer>; usage: { input_tokens?: number } | null };

/** Two questions that show the three types at once: an urgency scale, a
 * routing choice, and a yes/no. The ids are what the answers come back under. */
const DEFAULT_QUESTIONS: QuestionDraft[] = [
  {
    id: "urgence",
    type: "score",
    instructions: "À quel point ce message est-il urgent ?",
    options: "peut attendre\ncette semaine\ndans la journée\ntout de suite",
  },
  {
    id: "equipe",
    type: "choice",
    instructions: "Quelle équipe doit traiter ce message ?",
    options: "facturation\nlivraison\ntechnique",
  },
  { id: "mecontent", type: "noul", instructions: "Le client est-il mécontent ?", options: "" },
];

const STATUS_LABEL: Record<string, string> = {
  running: "En service",
  starting: "Démarrage…",
  stopped: "Arrêté",
  error: "Erreur au dernier lancement",
  unreachable: "Runner injoignable",
  unsupported: "Service de décision non déployé sur le runner",
};

/** What the server rendered: the service's state at load time. */
export type DecisionsSeed = { status: DecisionsStatus | null };

export default function DecisionsPage({ seed }: { seed?: Seed<DecisionsSeed> | null }) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const { data: initial } = useSeed<DecisionsSeed>(seed);
  const [status, setStatus] = useState<DecisionsStatus | null>(initial.status ?? null);
  const [stateText, setStateText] = useState("");
  const [isJson, setIsJson] = useState(false);
  const [drafts, setDrafts] = useState<QuestionDraft[]>(DEFAULT_QUESTIONS);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adminBusy, setAdminBusy] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await getJSON<DecisionsStatus>("/api/decisions"));
    } catch {
      setStatus(null);
    }
  }, []);

  const serviceUp = status?.status === "running";
  const statusLabel = t(STATUS_LABEL[status?.status ?? ""] ?? "État inconnu");

  const update = (index: number, patch: Partial<QuestionDraft>) => {
    setDrafts((current) => current.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  };

  const addQuestion = () => {
    if (drafts.length >= MAX_QUESTIONS) return;
    setDrafts((current) => [...current, { id: `question_${current.length + 1}`, type: "noul", instructions: "", options: "" }]);
  };

  const removeQuestion = (index: number) => {
    setDrafts((current) => current.filter((_, i) => i !== index));
  };

  const run = async () => {
    setError(null);
    const built = buildDecisionRequest(stateText, isJson, drafts);
    if (!built.ok) {
      setError(tServer(built.error, t));
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const answer = await sendJSON<RunResult & { ok: boolean; error?: string }>("/api/decisions/run", csrf, built.body);
      if (!answer.ok) {
        setError(answer.error ? tServer(answer.error, t) : t("La décision a échoué."));
        return;
      }
      setResult(answer as RunResult);
      void loadStatus();
    } catch {
      setError(t("La décision a échoué."));
    } finally {
      setBusy(false);
    }
  };

  const control = async (action: "start" | "stop") => {
    setAdminBusy(true);
    try {
      const answer = await sendJSON<{ ok?: boolean; error?: string }>(`/api/decisions/${action}`, csrf);
      if (answer.error) {
        setError(tServer(answer.error, t));
        showToast({ body: tServer(answer.error, t), type: "error" });
      } else {
        showToast({ body: t(action === "start" ? "Service de décision démarré." : "Service de décision arrêté."), type: "info" });
      }
      await loadStatus();
    } catch {
      setError(t("Le service de décision n'a pas répondu."));
    } finally {
      setAdminBusy(false);
    }
  };

  const answers = useMemo(() => (result ? Object.entries(result.answers) : []), [result]);

  return (
    <Layout>
      <LayoutContent>
        <VStack gap={5} width="100%">
          <VStack gap={1}>
            <Heading level={1}>{t("Décisions")}</Heading>
            <Text type="supporting" color="secondary">
              {t(
                "Pose des questions fermées à un modèle de décision : oui/non, choix parmi une liste, note sur une échelle. Il ne rédige pas de texte — il renvoie des probabilités.",
              )}
            </Text>
          </VStack>

          <HStack gap={3} vAlign="center" wrap="wrap">
            <StatusDot
              variant={
                serviceUp ? "success"
                  : status?.status === "starting" || status?.status === "unsupported" ? "warning"
                    : status?.status === "error" || !status ? "error" : "neutral"
              }
              label={statusLabel}
            />
            <Text type="supporting">{statusLabel}</Text>
            {status && (
              <Text type="supporting" color="secondary">
                {t("Modèle")} : {status.model}
                {status.port ? ` · ${t("port")} ${status.port}` : ""}
                {status.libre_gib !== null ? ` · ${t("mémoire libre")} ${status.libre_gib.toFixed(0)} Go` : ""}
              </Text>
            )}
            {status?.admin && (
              <HStack gap={2}>
                <Button
                  label={t("Actualiser l'état")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={ArrowPathIcon} size="sm" />}
                  onClick={() => void loadStatus()}
                />
                <Button
                  label={t("Démarrer")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PlayIcon} size="sm" />}
                  isLoading={adminBusy}
                  isDisabled={serviceUp}
                  onClick={() => void control("start")}
                />
                <Button
                  label={t("Arrêter")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={StopIcon} size="sm" />}
                  isLoading={adminBusy}
                  isDisabled={!serviceUp}
                  onClick={() => void control("stop")}
                />
              </HStack>
            )}
          </HStack>

          {status && !status.enabled && (
            <Banner
              status="warning"
              title={t("Modèle de décision non installé")}
              description={t(
                "Un administrateur doit télécharger le GGUF puis démarrer le service (DECISIONS_MODEL dans .env). La page reste utilisable dès qu'il tourne.",
              )}
            />
          )}

          <Card>
            <VStack gap={4}>
              <HStack gap={2} vAlign="center">
                <Icon icon={AdjustmentsHorizontalIcon} size="sm" />
                <Heading level={2}>{t("État à évaluer")}</Heading>
              </HStack>
              <SegmentedControl label={t("Format de l'état")} value={isJson ? "json" : "text"} onChange={(v) => setIsJson(v === "json")}>
                <SegmentedControlItem value="text" label={t("Texte")} />
                <SegmentedControlItem value="json" label={t("JSON")} />
              </SegmentedControl>
              <TextArea
                label={t("Contenu")}
                description={
                  isJson
                    ? t("Un objet ou un tableau JSON : il est envoyé tel quel au modèle.")
                    : t("Le message, le document ou l'extrait à évaluer.")
                }
                value={stateText}
                onChange={(value: string) => setStateText(value)}
                rows={6}
                maxLength={MAX_STATE_BYTES}
                placeholder={t("Colle ici le message, le ticket ou le document à évaluer…")}
              />
            </VStack>
          </Card>

          <Card>
            <VStack gap={4}>
              <HStack gap={2} vAlign="center">
                <Icon icon={ScaleIcon} size="sm" />
                <Heading level={2}>{t("Questions")}</Heading>
              </HStack>
              <Text type="supporting" color="secondary">
                {t("Chaque réponse revient sous l'identifiant de sa question. Les options d'une note vont du plus bas au plus haut.")}
              </Text>
              {drafts.map((draft, index) => (
                <VStack key={index} gap={2}>
                  <HStack gap={3} vAlign="end" wrap="wrap">
                    <TextInput
                      label={t("Identifiant de la question")}
                      value={draft.id}
                      onChange={(value: string) => update(index, { id: value })}
                      width={200}
                    />
                    <SegmentedControl
                      label={t("Type")}
                      value={draft.type}
                      onChange={(value) => update(index, { type: value as QuestionType })}
                    >
                      <SegmentedControlItem value="noul" label={t("Oui / non")} />
                      <SegmentedControlItem value="choice" label={t("Choix")} />
                      <SegmentedControlItem value="score" label={t("Note")} />
                    </SegmentedControl>
                    <Button
                      label={t("Retirer")}
                      variant="ghost"
                      size="sm"
                      icon={<Icon icon={TrashIcon} size="sm" />}
                      onClick={() => removeQuestion(index)}
                    />
                  </HStack>
                  <TextInput
                    label={t("Question posée")}
                    value={draft.instructions}
                    onChange={(value: string) => update(index, { instructions: value })}
                    width="100%"
                  />
                  {draft.type !== "noul" && (
                    <TextArea
                      label={draft.type === "choice" ? t("Choix possibles") : t("Niveaux, du plus bas au plus haut")}
                      description={t("Une valeur par ligne (ou séparées par des virgules).")}
                      value={draft.options}
                      onChange={(value: string) => update(index, { options: value })}
                      rows={3}
                    />
                  )}
                </VStack>
              ))}
              <HStack gap={2}>
                <Button
                  label={t("Ajouter une question")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  onClick={addQuestion}
                  isDisabled={drafts.length >= MAX_QUESTIONS}
                />
                <Badge label={t("{n} / {max}").replace("{n}", String(drafts.length)).replace("{max}", String(MAX_QUESTIONS))} />
              </HStack>
            </VStack>
          </Card>

          <HStack gap={3} vAlign="center">
            <Button
              label={t("Obtenir les décisions")}
              variant="primary"
              icon={<Icon icon={ScaleIcon} size="sm" />}
              isLoading={busy}
              isDisabled={!stateText.trim()}
              onClick={() => void run()}
            />
            {!serviceUp && status?.enabled && (
              <Text type="supporting" color="secondary">
                {t("Le service de décision n'est pas démarré : la requête sera refusée.")}
              </Text>
            )}
          </HStack>

          {error && <Banner status="error" title={t("Échec")} description={error} />}

          {answers.length === 0 && !error && (
            <EmptyState
              icon={<Icon icon={ScaleIcon} size="lg" />}
              title={t("Aucune décision pour le moment")}
              description={t("Renseigne un état et lance les questions : les réponses et leurs probabilités s'afficheront ici.")}
            />
          )}

          {answers.length > 0 && (
            <VStack gap={4}>
              <HStack gap={3} vAlign="center">
                <Heading level={2}>{t("Résultats")}</Heading>
                {result?.usage?.input_tokens !== undefined && (
                  <Text type="supporting" color="secondary">
                    {t("{n} tokens d'entrée").replace("{n}", String(result.usage.input_tokens))}
                  </Text>
                )}
              </HStack>
              {answers.map(([id, answer]) => {
                const draft = drafts.find((d) => d.id === id);
                const options = answerOptions(answer);
                return (
                  <Card key={id}>
                    <VStack gap={2}>
                      <HStack gap={3} vAlign="center" justify="between">
                        <VStack gap={0}>
                          <Text weight="semibold">{id}</Text>
                          {draft && <Text type="supporting" color="secondary">{draft.instructions}</Text>}
                        </VStack>
                        <HStack gap={2} vAlign="center">
                          <Text weight="semibold" size="lg">{answerHeadline(answer)}</Text>
                          {answer.confidence !== undefined && (
                            <Badge label={t("confiance {p}").replace("{p}", formatProbability(answer.confidence))} />
                          )}
                        </HStack>
                      </HStack>
                      {options.map(([label, probability]) => (
                        <Item
                          key={label}
                          density="compact"
                          label={label}
                          endContent={<Text type="supporting">{formatProbability(probability)}</Text>}
                        />
                      ))}
                    </VStack>
                  </Card>
                );
              })}
            </VStack>
          )}
        </VStack>
      </LayoutContent>
    </Layout>
  );
}
