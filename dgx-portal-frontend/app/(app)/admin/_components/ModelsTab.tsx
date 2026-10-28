"use client";

// Modèles — the model lifecycle: which service runs which model, what is in
// the catalog, and how to add to it. One action = one tab: « I want to launch
// or stop something » lands here.

import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Selector } from "@astryxdesign/core/Selector";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useToast } from "@astryxdesign/core/Toast";
import {
  PlayIcon,
  StopIcon,
  TrashIcon,
  PlusIcon,
  MagnifyingGlassIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import {
  SIDECAR_LABEL,
  SIDECAR_VARIANT,
  type ActResult,
  type AdminTabProps,
  type CatalogKind,
  type ModelCfg,
} from "./adminTypes";

/** The decision engine's states, named as the page names them. */
const DECISIONS_LABEL: Record<string, string> = {
  running: "En ligne",
  starting: "Démarrage…",
  stopped: "Arrêté",
  error: "Erreur",
  unsupported: "Service non déployé",
  unreachable: "Runner inaccessible",
};

export function ModelsTab({ data, act, actionDisabled, setConfirmAction }: AdminTabProps) {
  const t = useT();
  const [newOcr, setNewOcr] = useState({ name: "", hf_model_id: "", vllm_args: "" });
  const [newVoice, setNewVoice] = useState({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
  const [catalogKind, setCatalogKind] = useState<CatalogKind>("llm");
  // Catalog filter: the catalog grows, so the entries are matched on their
  // name AND on their identifier (hf_model_id / repo_id / image model id),
  // case-insensitively. It covers every kind that holds entries (LLM, OCR,
  // voice, image); the video notice is not a list, so it has nothing to
  // filter.
  const [catalogFilter, setCatalogFilter] = useState("");

  const st = data?.v_status.status;
  const q = catalogFilter.trim().toLowerCase();
  const match = (...fields: (string | null | undefined)[]) =>
    !q || fields.some((f) => (f ?? "").toLowerCase().includes(q));
  // The filtered lists, computed once per render (the grid and the « nothing
  // matches » line read the same result).
  const llmCfgs = (data?.model_cfgs ?? []).filter((cfg) => match(cfg.name, cfg.hf_model_id));
  const ocrCfgs = (data?.ocr_cfgs ?? []).filter((cfg) => match(cfg.name, cfg.hf_model_id));
  const voiceCfgs = (data?.voice_cfgs ?? []).filter((cfg) => match(cfg.name, cfg.repo_id));
  const imageIds = (data?.image_model_ids ?? []).filter((mid) => match(mid));
  const emptyCatalog = <Text type="supporting" color="secondary">{t("Aucun modèle ne correspond.")}</Text>;

  return (
    <VStack gap={6}>
      <VStack gap={3}>
        {/* A single row for the four backends: their status and
            start/stop were previously split between an isolated vLLM
            card and an OCR/video/voice grid. */}
        <Text weight="semibold">{t("Backends")}</Text>
        {data && (
          <Grid columns={{ minWidth: 200, max: 5 }} gap={3}>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={st === "running" ? "success" : st === "starting" ? "warning" : st === "error" ? "error" : "neutral"}
                      label={st === "running" ? t("En ligne") : st === "starting" ? t("Démarrage…") : st === "error" ? t("Erreur") : st === "unreachable" ? t("Runner inaccessible") : t("Arrêté")}
                    />
                    <Text weight="semibold">{t("LLM")}</Text>
                  </HStack>
                  {(st === "running" || st === "starting") && (
                    // Stopping the served model cuts ALL generations
                    // in flight: that deserves a dialog, not a direct click.
                    <Button
                      label={t("Arrêter")}
                      variant="secondary"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={StopIcon} size="sm" />}
                      isDisabled={actionDisabled}
                      onClick={() => setConfirmAction({ kind: "stop-model" })}
                    />
                  )}
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.v_status.model || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={SIDECAR_VARIANT[data.ocr_status] ?? "error"}
                      label={t(SIDECAR_LABEL[data.ocr_status] ?? "Injoignable")}
                    />
                    <Text weight="semibold">OCR</Text>
                  </HStack>
                  {data.ocr_status === "running" || data.ocr_status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/stop")} />
                  ) : (
                    <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/start")} />
                  )}
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.ocr_model_name || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={SIDECAR_VARIANT[data.video_status] ?? "error"}
                      label={t(SIDECAR_LABEL[data.video_status] ?? "Injoignable")}
                    />
                    <Text weight="semibold">{t("Vidéo")}</Text>
                  </HStack>
                  {data.video_status === "running" || data.video_status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/video/stop")} />
                  ) : (
                    <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/video/start")} />
                  )}
                </HStack>
                {/* A frozen ComfyUI workflow exposes no model id, but the
                    pipeline HAS a name: the card used to go green with nothing
                    under it, and stopped it said nothing at all. */}
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.video_model_name || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={SIDECAR_VARIANT[data.voice_status] ?? "error"}
                      label={t(SIDECAR_LABEL[data.voice_status] ?? "Injoignable")}
                    />
                    <Text weight="semibold">{t("Voix")}</Text>
                  </HStack>
                  {data.voice_status === "running" || data.voice_status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/stop")} />
                  ) : (
                    <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/start")} />
                  )}
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.voice_model_name || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={SIDECAR_VARIANT[data.asr_status] ?? "error"}
                      label={t(SIDECAR_LABEL[data.asr_status] ?? "Injoignable")}
                    />
                    <Text weight="semibold">{t("Dictée")}</Text>
                  </HStack>
                  {data.asr_status === "running" || data.asr_status === "starting"
                   || data.asr_status === "failed" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/asr/stop")} />
                  ) : (
                    <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/asr/start")} />
                  )}
                </HStack>
                {/* The name comes from the sidecar when it is loaded; stopped,
                    the card says so instead of showing a bare green/neutral
                    dot with no explanation. */}
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.asr_model_name || t("aucun modèle")}
                </Text>
                {/* The container runs WITHOUT a loaded model: it is not a
                    startup in progress but a failure, and the sidecar
                    publishes its cause (not enough unified memory on
                    2026-10-01). The button stays « Arrêter » — the
                    container holds the port — hence the reminder of the
                    order: stop, free, relaunch. */}
                {data.asr_load_error && (
                  <>
                    <Text type="supporting" color="secondary" wordBreak="break-all">
                      {t("Le modèle de dictée n'a pas pu se charger :")} {data.asr_load_error}
                    </Text>
                    <Text type="supporting" color="secondary">
                      {t("La mémoire est partagée avec le modèle de chat : libère de la mémoire, puis relance la dictée.")}
                    </Text>
                  </>
                )}
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={SIDECAR_VARIANT[data.image_status] ?? "error"}
                      label={t(SIDECAR_LABEL[data.image_status] ?? "Injoignable")}
                    />
                    <Text weight="semibold">{t("Image")}</Text>
                  </HStack>
                  {data.image_status === "running" || data.image_status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/stop")} />
                  ) : (
                    <Button label={t("Démarrer")} variant="primary" size="sm" isIconOnly icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/start")} />
                  )}
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.image_model_name || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
            <Card>
              <VStack gap={2}>
                <HStack hAlign="between" vAlign="center" gap={2}>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      variant={
                        data.decisions?.status === "running" ? "success"
                          : data.decisions?.status === "starting" || data.decisions?.status === "unsupported" ? "warning"
                            : data.decisions?.status === "error" ? "error" : "neutral"
                      }
                      label={t(DECISIONS_LABEL[data.decisions?.status ?? ""] ?? "Arrêté")}
                    />
                    <Text weight="semibold">{t("Décisions")}</Text>
                  </HStack>
                  {data.decisions?.status === "running" || data.decisions?.status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/api/decisions/stop")} />
                  ) : (
                    <Button
                      label={t("Démarrer")}
                      variant="primary"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={PlayIcon} size="sm" />}
                      isDisabled={actionDisabled || data.decisions?.enabled === false}
                      onClick={() => act("/api/decisions/start")}
                    />
                  )}
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.decisions?.status === "unsupported"
                    ? t("Mettre à jour le runner pour l'activer")
                    : data.decisions?.enabled === false
                      ? t("Modèle de décision non installé")
                      : data.decisions?.model || t("aucun modèle")}
                </Text>
              </VStack>
            </Card>
          </Grid>
        )}
      </VStack>

      <VStack gap={3}>
        {/* Single catalog: the chosen type drives both the displayed
            list and the add-form fields, instead of the three separate
            sections (vLLM / OCR / voice) from before. */}
        <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
          <HStack gap={3} vAlign="center">
            <Text weight="semibold">{t("Catalogue")}</Text>
            {catalogKind === "llm" ? (
              <AddModelDialog isDisabled={actionDisabled} onSubmit={(params) => act("/admin/model/add", params)} />
            ) : null}
          </HStack>
          <SegmentedControl label={t("Type de modèle")} value={catalogKind} onChange={(v) => setCatalogKind(v as CatalogKind)}>
            <SegmentedControlItem value="llm" label={t("LLM")} />
            <SegmentedControlItem value="ocr" label="OCR" />
            <SegmentedControlItem value="voice" label={t("Voix")} />
            <SegmentedControlItem value="image" label={t("Image")} />
            <SegmentedControlItem value="video" label={t("Vidéo")} />
            <SegmentedControlItem value="decisions" label={t("Décisions")} />
          </SegmentedControl>
        </HStack>

        {/* Text filter above the catalog: matches the NAME and the
            identifier (hf_model_id, repo_id, image model id). */}
        {(catalogKind === "llm" || catalogKind === "ocr" || catalogKind === "voice" || catalogKind === "image") && (
          <TextInput
            label={t("Filtrer le catalogue")}
            value={catalogFilter}
            onChange={setCatalogFilter}
            placeholder={t("Nom ou identifiant (ex : llama-3-8b, Qwen)")}
            startIcon={MagnifyingGlassIcon}
            hasClear
            size="sm"
          />
        )}

        {data && catalogKind === "llm" && (
          <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
            {llmCfgs.map((cfg) => (
              <Card key={cfg.id}>
                <VStack gap={2}>
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{cfg.name}</Text>
                    <Badge label={cfg.engine} variant="neutral" />
                  </HStack>
                  <Text type="supporting" color="secondary" wordBreak="break-all">
                    {cfg.hf_model_id}
                  </Text>
                  <HStack gap={2}>
                    {/* Lancer = load the model into memory (minutes,
                        heavy memory footprint) and Supprimer = remove
                        the entry (from the catalog AND the LiteLLM
                        routing): both go through a confirmation. */}
                    <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => setConfirmAction({ kind: "launch", name: cfg.name })} />
                    <ArgsEditForm
                      cfg={cfg}
                      isDisabled={actionDisabled}
                      onSubmit={(params) => act(`/admin/model/edit/${cfg.id}`, params)}
                    />
                    <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => setConfirmAction({ kind: "delete-model", id: cfg.id, name: cfg.name })} />
                  </HStack>
                </VStack>
              </Card>
            ))}
            {llmCfgs.length === 0 && emptyCatalog}
          </Grid>
        )}

        {data && catalogKind === "decisions" && (
          <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
            <Card>
              <VStack gap={2}>
                <HStack gap={2} vAlign="center">
                  <Text weight="semibold">{data.decisions?.name || t("Décisions")}</Text>
                  <Badge label="decisions" variant="neutral" />
                </HStack>
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.decisions?.ref || t("Définir DECISIONS_MODEL dans .env")}
                </Text>
                {/* The arguments come from DECISIONS_ARGS, not from a form: shown
                    as they are, so what runs is what is read. */}
                <Text type="supporting" color="secondary" wordBreak="break-all">
                  {data.decisions?.args || t("Arguments définis par DECISIONS_ARGS")}
                </Text>
                <HStack gap={2}>
                  {data.decisions?.status === "running" || data.decisions?.status === "starting" ? (
                    <Button label={t("Arrêter")} variant="secondary" size="sm" icon={<Icon icon={StopIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/api/decisions/stop")} />
                  ) : (
                    <Button
                      label={t("Lancer")}
                      variant="primary"
                      size="sm"
                      icon={<Icon icon={PlayIcon} size="sm" />}
                      isDisabled={actionDisabled || data.decisions?.enabled === false}
                      onClick={() => act("/api/decisions/start")}
                    />
                  )}
                </HStack>
              </VStack>
            </Card>
          </Grid>
        )}

        {data && catalogKind === "ocr" && (
          <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
            {ocrCfgs.map((cfg) => (
              <Card key={cfg.id}>
                <VStack gap={2}>
                  <Text weight="semibold">{cfg.name}</Text>
                  <Text type="supporting" color="secondary" wordBreak="break-all">
                    {cfg.hf_model_id}
                  </Text>
                  <HStack gap={2}>
                    <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/ocr/catalog/launch", { ocr_name: cfg.name })} />
                    <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/ocr/catalog/delete/${cfg.id}`)} />
                  </HStack>
                </VStack>
              </Card>
            ))}
            {ocrCfgs.length === 0 && emptyCatalog}
            <Card>
              <VStack gap={2}>
                <Text type="supporting" color="secondary">{t("Ajouter un modèle OCR")}</Text>
                <TextInput label={t("Nom")} isLabelHidden value={newOcr.name} onChange={(v) => setNewOcr((s) => ({ ...s, name: v }))} placeholder={t("Nom (ex: unlimited-ocr)")} size="sm" />
                <TextInput label={t("HF ID")} isLabelHidden value={newOcr.hf_model_id} onChange={(v) => setNewOcr((s) => ({ ...s, hf_model_id: v }))} placeholder={t("HF ID")} size="sm" />
                <TextInput label={t("Args")} isLabelHidden value={newOcr.vllm_args} onChange={(v) => setNewOcr((s) => ({ ...s, vllm_args: v }))} placeholder={t("Args du moteur")} size="sm" />
                <Button
                  label={t("Ajouter")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  onClick={async () => {
                    await act("/admin/ocr/catalog/add", newOcr);
                    setNewOcr({ name: "", hf_model_id: "", vllm_args: "" });
                  }}
                  isDisabled={actionDisabled}
                />
              </VStack>
            </Card>
          </Grid>
        )}

        {data && catalogKind === "voice" && (
          <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
            {voiceCfgs.map((cfg) => (
              <Card key={cfg.id}>
                <VStack gap={2}>
                  <Text weight="semibold">{cfg.name}</Text>
                  <Text type="supporting" color="secondary" wordBreak="break-all">
                    {cfg.repo_id}
                  </Text>
                  <HStack gap={2}>
                    <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/voice/catalog/launch", { voice_name: cfg.name })} />
                    <Button label={t("Supprimer")} variant="secondary" size="sm" isIconOnly icon={<Icon icon={TrashIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act(`/admin/voice/catalog/delete/${cfg.id}`)} />
                  </HStack>
                </VStack>
              </Card>
            ))}
            {voiceCfgs.length === 0 && emptyCatalog}
            <Card>
              <VStack gap={2}>
                <Text type="supporting" color="secondary">{t("Ajouter un modèle voix")}</Text>
                <TextInput label={t("Nom")} isLabelHidden value={newVoice.name} onChange={(v) => setNewVoice((s) => ({ ...s, name: v }))} placeholder={t("Nom (ex: qwen3-tts)")} size="sm" />
                <Selector
                  label={t("Variante")}
                  isLabelHidden
                  value={newVoice.repo_id}
                  onChange={(v) => setNewVoice((s) => ({ ...s, repo_id: v ?? "Qwen3-TTS-12Hz-1.7B-Base" }))}
                  options={[
                    { value: "kyutai/tts-1.6b-en_fr", label: t("Kyutai TTS 1.6B (FR/EN, mode vocal)") },
                    { value: "Qwen3-TTS-12Hz-1.7B-Base", label: t("Qwen3-TTS 1.7B (10 langues)") },
                    { value: "Qwen3-TTS-12Hz-0.6B-Base", label: t("Qwen3-TTS 0.6B (10 langues)") },
                    { value: "chatterbox-multilingual", label: "Chatterbox Multilingual (0.5B)" },
                    { value: "chatterbox-turbo", label: "Chatterbox Turbo (350M, EN)" },
                    { value: "chatterbox", label: "Chatterbox Original (0.5B, EN)" },
                  ]}
                  size="sm"
                />
                <Button
                  label={t("Ajouter")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  onClick={async () => {
                    await act("/admin/voice/catalog/add", newVoice);
                    setNewVoice({ name: "", repo_id: "Qwen3-TTS-12Hz-1.7B-Base" });
                  }}
                  isDisabled={actionDisabled}
                />
              </VStack>
            </Card>
          </Grid>
        )}

        {data && catalogKind === "image" && (
          <Grid columns={{ minWidth: 240, max: 4 }} gap={3}>
            {imageIds.map((mid) => (
              <Card key={mid}>
                <VStack gap={2}>
                  <Text weight="semibold" wordBreak="break-all">{mid}</Text>
                  <Text type="supporting" color="secondary">
                    {mid === data.image_model_name ? t("Modèle actif") : t("diffusers · text-to-image")}
                  </Text>
                  <HStack gap={2}>
                    <Button label={t("Lancer")} variant="primary" size="sm" icon={<Icon icon={PlayIcon} size="sm" />} isDisabled={actionDisabled} onClick={() => act("/admin/image/launch", { model_id: mid })} />
                  </HStack>
                </VStack>
              </Card>
            ))}
            {imageIds.length === 0 && emptyCatalog}
            <Card>
              <VStack gap={1}>
                <Text type="supporting" color="secondary">{t("Ajouter un modèle image")}</Text>
                <Text type="supporting" color="secondary">
                  {t("Les poids image (gated, ~35 Go) se téléchargent côté hôte puis s'ajoutent à la liste blanche — même principe que l'OCR/voix. Lance ensuite le modèle ci-contre.")}
                </Text>
              </VStack>
            </Card>
          </Grid>
        )}

        {data && catalogKind === "video" && (
          <Card>
            <VStack gap={1}>
              <Text type="supporting" color="secondary">
                {t("La vidéo n'a pas de catalogue : un seul workflow ComfyUI figé, démarré et arrêté depuis la ligne « Backends » ci-dessus.")}
              </Text>
            </VStack>
          </Card>
        )}
      </VStack>
    </VStack>
  );
}

/** Make the args of a catalog entry editable (POST
 * /admin/model/edit/<id>): they were frozen at creation time. Name, HF ID and
 * engine are displayed read-only — they are what identifies the entry,
 * the route only changes the args. The backend `warning` field (LiteLLM
 * routing not refreshed) is shown when present. */
function ArgsEditForm({ cfg, isDisabled, onSubmit }: {
  cfg: ModelCfg;
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<ActResult>;
}) {
  const t = useT();
  const showToast = useToast();
  const [isOpen, setIsOpen] = useState(false);
  const [args, setArgs] = useState(cfg.vllm_args);
  return (
    <>
      <Button
        label={t("Args")}
        variant="secondary"
        size="sm"
        isDisabled={isDisabled}
        onClick={() => { setArgs(cfg.vllm_args); setIsOpen(true); }}
      />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Modifier les args du moteur")} subtitle={`${cfg.name} · ${cfg.hf_model_id} · ${cfg.engine}`} />
        <VStack gap={3}>
          <Text type="supporting" color="secondary">{t("Seuls les args du moteur changent ; l'entrée reste identifiée par son nom et son HF ID.")}</Text>
          <TextInput label={t("Args du moteur")} value={args} onChange={setArgs} size="sm" />
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Enregistrer")}
              variant="primary"
              size="sm"
              isDisabled={isDisabled}
              onClick={async () => {
                const res = await onSubmit({
                  name: cfg.name,
                  hf_model_id: cfg.hf_model_id,
                  vllm_args: args,
                  engine: cfg.engine,
                });
                if (res.ok) {
                  setIsOpen(false);
                  // The args are saved but LiteLLM did not follow:
                  // the entry stays routed with old context limits.
                  if (res.warning) showToast({ body: res.warning, type: "error" });
                }
                // On failure, the dialog stays open (the error is already toasted
                // by act()): the admin can fix and resubmit.
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}

/** What each engine expects, shown in the « add a model » dialog: the kind of
 *  identifier and an example of arguments (the runner allow-lists them). */
const ENGINES: Record<string, { label: string; id: string; exampleId: string; args: string }> = {
  vllm: {
    label: "vLLM (safetensors)",
    id: "Dépôt Hugging Face (organisation/modèle).",
    exampleId: "Qwen/Qwen3-8B",
    args: "--max-model-len 32768 --gpu-memory-utilization 0.85",
  },
  "vllm-docker": {
    label: "vLLM (conteneur Docker)",
    id: "Dépôt Hugging Face (organisation/modèle).",
    exampleId: "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
    args: "--max-model-len 1048576 --max-num-seqs 4 --gpu-memory-utilization 0.75",
  },
  llamacpp: {
    label: "llama.cpp (GGUF)",
    id: "Dépôt Hugging Face, ou local:<dossier> pour un GGUF déjà téléchargé.",
    exampleId: "local:qwen38-27b-gguf",
    args: "--ctx-size 262144 --parallel 4 --n-gpu-layers 999 --flash-attn on",
  },
  ds4: {
    label: "ds4 (GGUF NVFP4)",
    id: "Dépôt Hugging Face, ou local:<dossier> pour un GGUF déjà téléchargé.",
    exampleId: "local:deepseek-v4-flash",
    args: "--ctx-size 131072",
  },
};

/** « Ajouter un modèle » (POST /admin/model/add) in a dialog: the inline card
 *  squeezed four unlabelled fields into a catalog tile, with nothing saying
 *  what an « HF ID » or the engine args look like for the chosen engine. */
function AddModelDialog({ isDisabled, onSubmit }: {
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<ActResult>;
}) {
  const t = useT();
  const empty = { name: "", hf_model_id: "", engine: "vllm", vllm_args: "" };
  const [isOpen, setIsOpen] = useState(false);
  const [m, setM] = useState(empty);
  const engine = ENGINES[m.engine] ?? ENGINES.vllm;
  return (
    <>
      <Button
        label={t("Ajouter un modèle")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={PlusIcon} size="sm" />}
        isDisabled={isDisabled}
        onClick={() => { setM(empty); setIsOpen(true); }}
      />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader title={t("Ajouter un modèle")} subtitle={t("Le modèle rejoint le catalogue ; il se lance ensuite depuis sa carte.")} />
        <VStack gap={3}>
          <Selector
            label={t("Moteur")}
            value={m.engine}
            onChange={(v) => setM((s) => ({ ...s, engine: v ?? "vllm" }))}
            options={Object.entries(ENGINES).map(([value, e]) => ({ value, label: e.label }))}
          />
          <TextInput
            label={t("Nom")}
            description={t("Nom affiché, et nom du modèle dans l'API.")}
            value={m.name}
            onChange={(v) => setM((s) => ({ ...s, name: v }))}
            placeholder={t("ex. {v}").replace("{v}", "qwen3-8b")}
            size="sm"
          />
          <TextInput
            label={t("Identifiant du modèle")}
            description={t(engine.id)}
            value={m.hf_model_id}
            onChange={(v) => setM((s) => ({ ...s, hf_model_id: v }))}
            placeholder={t("ex. {v}").replace("{v}", engine.exampleId)}
            size="sm"
          />
          <TextInput
            label={t("Args du moteur (optionnels)")}
            description={t("Le runner les vérifie au lancement : une option non autorisée y est refusée.")}
            value={m.vllm_args}
            onChange={(v) => setM((s) => ({ ...s, vllm_args: v }))}
            placeholder={t("ex. {v}").replace("{v}", engine.args)}
            size="sm"
          />
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Ajouter")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isDisabled={isDisabled || !m.name.trim() || !m.hf_model_id.trim()}
              onClick={async () => {
                const res = await onSubmit({ ...m, name: m.name.trim(), hf_model_id: m.hf_model_id.trim() });
                // On failure the dialog stays open (act() toasts the reason).
                if (res.ok) setIsOpen(false);
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}
