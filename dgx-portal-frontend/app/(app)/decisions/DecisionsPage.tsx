"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { FileInput } from "@astryxdesign/core/FileInput";
import { Thumbnail } from "@astryxdesign/core/Thumbnail";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Card } from "@astryxdesign/core/Card";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Divider } from "@astryxdesign/core/Divider";
import { Icon } from "@astryxdesign/core/Icon";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowPathIcon,
  BeakerIcon,
  CodeBracketIcon,
  CubeIcon,
  DocumentTextIcon,
  PhotoIcon,
  PlayIcon,
  SparklesIcon,
  StopIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import { getJSON, sendJSON } from "@/lib/api";
import { useSeed, type Seed } from "@/lib/seed";
import { useCsrf } from "@/lib/useCsrf";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { tServer, useT } from "@/lib/i18n";
import { DEFAULT_EXAMPLE, EXAMPLES, type DecisionExample } from "@/lib/decision-examples";
import {
  answerHeadline,
  answerOptions,
  answerScale,
  answerTypeLabel,
  formatProbability,
  prettyJson,
  validatePayload,
  type DecisionAnswer,
} from "@/lib/decisions";

type DecisionsStatus = {
  model: string;
  enabled: boolean;
  status: string;
  port: number | null;
  libre_gib: number | null;
  ref?: string;
  admin?: boolean;
};

type RunResult = { ok: true; model: string; answers: Record<string, DecisionAnswer>; usage: { input_tokens?: number } | null };

const STATUS_LABEL: Record<string, string> = {
  running: "En service",
  starting: "Démarrage…",
  stopped: "Arrêté",
  error: "Erreur au dernier lancement",
  unsupported: "Service non déployé sur le runner",
  unreachable: "Runner injoignable",
};

/** The three question types, shown while the editor is still empty. */
const TYPE_HINTS: { type: string; description: string; example: string }[] = [
  { type: "noul", description: "Une réponse oui / non, avec sa probabilité.", example: "Le client demande-t-il un remboursement ?" },
  { type: "choice", description: "Une option parmi une liste fermée.", example: "Quelle équipe doit traiter ce message ?" },
  { type: "score", description: "Une position sur une échelle ordonnée.", example: "À quel point est-ce urgent ?" },
];

const STATE_PLACEHOLDER = `{
  "commande": { "id": 4411, "debite_deux_fois": true },
  "message": "Ma commande a été débitée deux fois…"
}`;

const QUESTIONS_PLACEHOLDER = `{
  "remboursement": {
    "type": "noul",
    "instructions": "Le client demande-t-il un remboursement ?"
  }
}`;

/** The mono style of a code editor, from the theme's code font token. */
const CODE: React.CSSProperties = { fontFamily: "var(--font-family-code)" };

export type DecisionsSeed = { status: DecisionsStatus | null };

export default function DecisionsPage({ seed }: { seed?: Seed<DecisionsSeed> | null }) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const isNarrow = useIsNarrow();
  const { data: initial } = useSeed<DecisionsSeed>(seed);
  const [status, setStatus] = useState<DecisionsStatus | null>(initial.status ?? null);
  const [stateText, setStateText] = useState("");
  const [questionsText, setQuestionsText] = useState("");
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adminBusy, setAdminBusy] = useState(false);
  // The right column shows one thing at a time: the examples browser, the
  // result, or what to do first. Running a request closes the browser.
  const [examplesOpen, setExamplesOpen] = useState(false);
  // Images attached to the request. The model reads them (llama.cpp `files`),
  // and they show as thumbnails under the state and beside the questions — the
  // place where their references are written.
  const [assets, setAssets] = useState<{ name: string; dataUrl: string }[]>([]);
  const [inputFiles, setInputFiles] = useState<File | File[] | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await getJSON<DecisionsStatus>("/api/decisions"));
    } catch {
      setStatus(null);
    }
  }, []);

  const run = useCallback(async () => {
    setError(null);
    const built = validatePayload(stateText, questionsText, assets.map((a) => a.dataUrl));
    if (!built.ok) {
      setError(tServer(built.error, t));
      setExamplesOpen(false);
      return;
    }
    setBusy(true);
    setExamplesOpen(false);
    setResult(null);
    try {
      const answer = await sendJSON<RunResult & { ok: boolean; error?: string }>("/api/decisions/run", csrf, built.body);
      if (!answer.ok) {
        setError(answer.error ? tServer(answer.error, t) : t("La décision a échoué."));
        return;
      }
      setResult(answer as RunResult);
    } catch {
      setError(t("La décision a échoué."));
    } finally {
      setBusy(false);
    }
  }, [assets, csrf, questionsText, stateText, t]);

  // Ctrl/⌘ + ↵ launches the request, like the System One playground. The ref
  // holds the current `run` so the listener is installed once.
  const runRef = useRef(run);
  useEffect(() => {
    runRef.current = run;
  }, [run]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        void runRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const control = async (action: "start" | "stop") => {
    setAdminBusy(true);
    try {
      const answer = await sendJSON<{ ok?: boolean; error?: string }>(`/api/decisions/${action}`, csrf);
      if (answer.error) {
        const message = tServer(answer.error, t);
        setError(message);
        showToast({ body: message, type: "error" });
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

  const format = (which: "state" | "questions") => {
    const pretty = prettyJson(which === "state" ? stateText : questionsText);
    if (pretty === null) {
      setError(t("Ce JSON ne se lit pas : rien à formater."));
      return;
    }
    if (which === "state") setStateText(pretty);
    else setQuestionsText(pretty);
  };

  /** Read the picked files into data URLs: that is what the engine takes, and
   *  what a thumbnail can display. */
  const onPickFiles = async (picked: File | File[] | null) => {
    // The control keeps no value: the thumbnails below ARE the selection, and
    // its own chip repeated the file name a second time.
    setInputFiles(null);
    const list = picked === null ? [] : Array.isArray(picked) ? picked : [picked];
    const read = await Promise.all(
      list.map(
        (file) =>
          new Promise<{ name: string; dataUrl: string } | null>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve({ name: file.name, dataUrl: String(reader.result) });
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(file);
          }),
      ),
    );
    setAssets(read.filter((a): a is { name: string; dataUrl: string } => a !== null));
  };

  /** Load an example into the editors and get out of the way. */
  const pickExample = (example: DecisionExample) => {
    setStateText(example.state);
    setQuestionsText(example.questions);
    setExamplesOpen(false);
    setResult(null);
    setError(null);
  };

  /** The instructions of each question, read back from the editor: they are
   *  what makes a result row readable ("Why is this urgent?"), and the engine
   *  does not echo them. */
  const instructions = useMemo(() => {
    try {
      const parsed = JSON.parse(questionsText) as Record<string, { instructions?: unknown }>;
      return Object.fromEntries(
        Object.entries(parsed).map(([id, q]) => [id, typeof q?.instructions === "string" ? q.instructions : ""]),
      );
    } catch {
      return {} as Record<string, string>;
    }
  }, [questionsText]);

  const answers = useMemo(() => (result ? Object.entries(result.answers) : []), [result]);
  const serviceUp = status?.status === "running";
  const statusLabel = t(STATUS_LABEL[status?.status ?? ""] ?? "État inconnu");
  const questionsEmpty = questionsText.trim() === "";

  return (
    <Layout height="fill">
      <LayoutContent padding={6}>
        <VStack gap={4} width="100%">
          <HStack gap={3} vAlign="center" justify="between" wrap="wrap">
            <VStack gap={0}>
              <Heading level={1}>{t("Décisions")}</Heading>
              <Text type="supporting" color="secondary">
                {t(
                  "Un modèle de décision lit un état et répond à des questions typées. Il ne rédige pas de texte : il renvoie des probabilités.",
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
                  {status.model}
                  {status.port ? ` · ${t("port")} ${status.port}` : ""}
                  {status.libre_gib !== null ? ` · ${status.libre_gib.toFixed(0)} ${t("Go libres")}` : ""}
                </Text>
              )}
              <Button
                label={t("Actualiser l'état")}
                variant="ghost"
                size="sm"
                icon={<Icon icon={ArrowPathIcon} size="sm" />}
                onClick={() => void loadStatus()}
              />
              {status?.admin && (
                <Button
                  label={t(serviceUp ? "Arrêter" : "Démarrer")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={serviceUp ? StopIcon : PlayIcon} size="sm" />}
                  isLoading={adminBusy}
                  onClick={() => void control(serviceUp ? "stop" : "start")}
                />
              )}
            </HStack>
          </HStack>

          <HStack gap={6} vAlign="start" width="100%" wrap={isNarrow ? "wrap" : "nowrap"}>
            {/* ── Left: the request, as JSON ─────────────────────────────── */}
            <VStack gap={4} width={isNarrow ? "100%" : 460}>
              <VStack gap={1}>
                <HStack gap={2} vAlign="center" justify="between">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={CodeBracketIcon} size="sm" />
                    <Text weight="semibold">{t("État")}</Text>
                  </HStack>
                  <Button label={t("Formater")} variant="ghost" size="sm" onClick={() => format("state")} isDisabled={!stateText.trim()} />
                </HStack>
                <TextArea
                  label={t("État")}
                  isLabelHidden
                  value={stateText}
                  onChange={setStateText}
                  rows={8}
                  hasSpellCheck={false}
                  placeholder={STATE_PLACEHOLDER}
                  style={CODE}
                />
                {/* Images travel WITH the state: the engine reads them, so the
                    picker lives here and the previews show what it will see. */}
                <HStack gap={2} vAlign="center" wrap="wrap">
                  <FileInput
                    label={t("Images jointes")}
                    isLabelHidden
                    value={inputFiles}
                    onChange={(files) => void onPickFiles(files)}
                    accept="image/*"
                    isMultiple
                    maxFiles={4}
                    maxSize={4 * 1024 * 1024}
                    placeholder={t("Ajouter une image")}
                  />
                  {assets.map((asset) => (
                    <Thumbnail
                      key={asset.name}
                      src={asset.dataUrl}
                      alt={asset.name}
                      label={asset.name}
                      onRemove={() => {
                        setAssets((current) => current.filter((a) => a.name !== asset.name));
                        setInputFiles(null);
                      }}
                    />
                  ))}
                  {assets.length > 0 && (
                    <Text type="supporting" color="secondary">
                      {t("{n} image(s) lue(s) par le modèle").replace("{n}", String(assets.length))}
                    </Text>
                  )}
                </HStack>
              </VStack>

              <VStack gap={1}>
                <HStack gap={2} vAlign="center" justify="between">
                  <HStack gap={2} vAlign="center">
                    <Icon icon={SparklesIcon} size="sm" />
                    <Text weight="semibold">{t("Questions")}</Text>
                    <Badge label={String(Object.keys(instructions).length)} />
                  </HStack>
                  <Button label={t("Formater")} variant="ghost" size="sm" onClick={() => format("questions")} isDisabled={!questionsText.trim()} />
                </HStack>
                <TextArea
                  label={t("Questions")}
                  isLabelHidden
                  value={questionsText}
                  onChange={setQuestionsText}
                  rows={questionsEmpty ? 4 : 12}
                  hasSpellCheck={false}
                  placeholder={QUESTIONS_PLACEHOLDER}
                  style={CODE}
                />
                {assets.length > 0 && (
                  <HStack gap={2} vAlign="center" wrap="wrap">
                    {assets.map((asset) => (
                      <Thumbnail key={asset.name} src={asset.dataUrl} alt={asset.name} label={asset.name} />
                    ))}
                    <Text type="supporting" color="secondary">
                      {t("Ces images sont lues : nomme-les dans les questions (« image 1 », « image 2 »…).")}
                    </Text>
                  </HStack>
                )}
                {/* The three types, explained where they are needed: an empty
                    editor says nothing about what can go in it. */}
                {questionsEmpty && (
                  <Grid columns={{ minWidth: 120, max: 3 }} gap={2}>
                    {TYPE_HINTS.map((hint) => (
                      <Card key={hint.type}>
                        <VStack gap={0.5}>
                          <Badge label={hint.type} />
                          <Text type="supporting">{t(hint.description)}</Text>
                          <Text type="supporting" color="secondary" style={{ fontStyle: "italic" }}>{t(hint.example)}</Text>
                        </VStack>
                      </Card>
                    ))}
                  </Grid>
                )}
              </VStack>

              <HStack gap={3} vAlign="center">
                <Button
                  label={t("Lancer la requête")}
                  variant="primary"
                  icon={<Icon icon={BeakerIcon} size="sm" />}
                  isLoading={busy}
                  isDisabled={!stateText.trim()}
                  onClick={() => void run()}
                />
                <Text type="supporting" color="secondary">{t("Ctrl + ↵")}</Text>
                <StackItem>
                  <HStack justify="end">
                    <Button
                      label={t("Des exemples")}
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setExamplesOpen(true);
                        setError(null);
                      }}
                    />
                  </HStack>
                </StackItem>
              </HStack>
            </VStack>

            {/* ── Right: examples, then the answers ──────────────────────── */}
            <StackItem size="fill" style={{ minWidth: 0 }}>
              <VStack gap={3} width="100%">
                {examplesOpen ? (
                  <ExamplesPanel onPick={pickExample} onClose={() => setExamplesOpen(false)} />
                ) : (
                  <>
                    <HStack gap={2} vAlign="center" justify="between">
                      <Text weight="semibold">{t("Réponses")}</Text>
                      <HStack gap={3} vAlign="center">
                        {result?.usage?.input_tokens !== undefined && (
                          <Text type="supporting" color="secondary">
                            {t("{n} tokens d'entrée").replace("{n}", String(result.usage.input_tokens))}
                          </Text>
                        )}
                        <Text type="supporting" color="secondary">{t("Type")}</Text>
                      </HStack>
                    </HStack>
                    <Divider />

                    {error && <Banner status="error" title={t("Échec")} description={error} />}

                    {answers.length === 0 && !error && (
                      <VStack gap={3} hAlign="center" vAlign="center" padding={8}>
                        <Icon icon={CubeIcon} size="lg" />
                        <Text weight="semibold">{t("Lance une requête pour voir la sortie")}</Text>
                        <Button
                          label={t("Voir des exemples de requêtes")}
                          variant="primary"
                          onClick={() => setExamplesOpen(true)}
                        />
                      </VStack>
                    )}

                    {answers.map(([id, answer]) => (
                      <AnswerRow key={id} id={id} answer={answer} instruction={instructions[id]} />
                    ))}
                  </>
                )}
              </VStack>
            </StackItem>
          </HStack>
        </VStack>
      </LayoutContent>
    </Layout>
  );
}

/** One answer: what was asked, what was decided, how sure, and the field. */
function AnswerRow({ id, answer, instruction }: { id: string; answer: DecisionAnswer; instruction?: string }) {
  const t = useT();
  const options = answerOptions(answer);
  const confidence = answer.confidence;
  const scale = answerScale(answer);
  const noul = typeof answer.noul === "number" ? answer.noul : null;

  return (
    <VStack gap={2} width="100%">
      <HStack gap={3} vAlign="start" justify="between">
        <VStack gap={0}>
          <Text weight="semibold">{id}</Text>
          {instruction && (
            <Text type="supporting" color="secondary" style={{ fontStyle: "italic" }}>{instruction}</Text>
          )}
        </VStack>
        <VStack gap={0} hAlign="end">
          <Text type="supporting">{answerTypeLabel(answer)}</Text>
          {scale && <Text type="supporting" color="secondary">{scale}</Text>}
          {answer.type === "choice" && options.length > 0 && (
            <Text type="supporting" color="secondary">
              {t("{n} options").replace("{n}", String(options.length))}
            </Text>
          )}
        </VStack>
      </HStack>

      {noul !== null && (
        <VStack gap={1} width="100%">
          <Text weight="semibold" size="lg">{answerHeadline(answer)}</Text>
          <ProgressBar label={id} isLabelHidden value={noul * 100} variant={noul >= 0.5 ? "success" : "neutral"} />
        </VStack>
      )}

      {typeof answer.score === "number" && <Text weight="semibold" size="lg">{answerHeadline(answer)}</Text>}

      {options.map(([label, probability]) => (
        <VStack key={label} gap={0.5} width="100%">
          <HStack gap={2} justify="between" vAlign="center">
            <Text type="supporting">{label}</Text>
            <Text type="supporting">{formatProbability(probability)}</Text>
          </HStack>
          <ProgressBar label={label} isLabelHidden value={probability * 100} variant="accent" />
        </VStack>
      ))}

      {confidence !== undefined && (
        <Text type="supporting" color="secondary">
          {t("Confiance : {p}").replace("{p}", formatProbability(confidence))}
        </Text>
      )}
      <Divider />
    </VStack>
  );
}

/**
 * The examples browser, on the right where the answers appear: the same place,
 * so choosing an example does not move the eye. Picking one fills the editors
 * and closes it.
 */
function ExamplesPanel({ onPick, onClose }: { onPick: (example: DecisionExample) => void; onClose: () => void }) {
  const t = useT();
  return (
    <VStack gap={4} width="100%">
      <HStack gap={2} vAlign="center" justify="between">
        <Text type="supporting" color="secondary">{t("MODÈLE DE DÉCISION · CLEF Q4_K_M")}</Text>
        <Button
          label={t("Fermer")}
          variant="ghost"
          size="sm"
          isIconOnly
          icon={<Icon icon={XMarkIcon} size="sm" />}
          onClick={onClose}
        />
      </HStack>

      <VStack gap={1}>
        <Heading level={2}>{t("Apprendre au modèle à décider")}</Heading>
        <Text type="supporting" color="secondary">
          {t("Choisis un point de départ : l'état et les questions sont chargés dans les deux éditeurs, à toi de les adapter.")}
        </Text>
      </VStack>

      <Card>
        <VStack gap={1}>
          <Text weight="semibold">{t("Une décision, pas une rédaction")}</Text>
          <Text type="supporting" color="secondary">
            {t("Le modèle ne rédige pas de réponse : il rend une probabilité par option, et sa confiance dans ce choix.")}
          </Text>
        </VStack>
      </Card>

      <VStack gap={2}>
        <Text type="supporting" color="secondary">{t("CE QUE LE MODÈLE SAIT FAIRE")}</Text>
        <Grid columns={{ minWidth: 180, max: 2 }} gap={3}>
          <Card>
            <VStack gap={1}>
              <Icon icon={DocumentTextIcon} size="sm" />
              <Text weight="semibold">{t("Trois formes de question")}</Text>
              <Text type="supporting" color="secondary">{t("Oui/non, un choix parmi une liste, ou une note sur une échelle.")}</Text>
            </VStack>
          </Card>
          <Card>
            <VStack gap={1}>
              <Icon icon={PhotoIcon} size="sm" />
              <Text weight="semibold">{t("Les images, pas encore")}</Text>
              <Text type="supporting" color="secondary">
                {t("Ce modèle-ci lit du texte seulement : un exemple avec image est envoyé comme du texte.")}
              </Text>
            </VStack>
          </Card>
        </Grid>
      </VStack>

      <VStack gap={2}>
        <Text type="supporting" color="secondary">{t("EXEMPLES")}</Text>
        {EXAMPLES.map((example) => (
          <ClickableCard
            key={example.id}
            label={t(example.title)}
            variant="muted"
            onClick={() => onPick(example)}
          >
            <HStack gap={3} vAlign="center" justify="between" width="100%">
              <HStack gap={3} vAlign="center">
                <Icon icon={example.kind === "image" ? PhotoIcon : DocumentTextIcon} size="sm" />
                <VStack gap={0}>
                  <Text weight="semibold">{t(example.title)}</Text>
                  <Text type="supporting" color="secondary">{t(example.description)}</Text>
                </VStack>
              </HStack>
              <Badge label={t(example.kind === "image" ? "Image" : "Texte")} />
            </HStack>
          </ClickableCard>
        ))}
      </VStack>

      <HStack justify="end">
        <Button label={t("Charger l'exemple")} variant="secondary" size="sm" onClick={() => onPick(DEFAULT_EXAMPLE)} />
      </HStack>
    </VStack>
  );
}
