"use client";

// Playground settings panel, rendered in the Popover anchored to the gear
// wheel: no Card here (the popover provides the surface — an extra Card
// would make a double frame). Titled sections to pace the reading:
// system prompt (persona + text), generation, reasoning.
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Slider } from "@astryxdesign/core/Slider";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { Selector } from "@astryxdesign/core/Selector";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import type { Settings } from "@/lib/types";
import { useMemo } from "react";
import { useT } from "@/lib/i18n";

// Personas: pre-written system prompts, chosen in a dropdown menu rather
// than a row of buttons. The prompts are in English (system instructions),
// the UI stays in French.
const PERSONAS: { id: string; label: string; prompt: string }[] = [
  { id: "code", label: "Code", prompt: "You are a senior software engineer. Produce complete, runnable, idiomatic code. Prefer whole files over snippets and never elide parts of a file." },
  { id: "redacteur", label: "Rédacteur", prompt: "You are a careful writer. Be clear, structured, and concise. Favour short paragraphs and useful headings." },
  { id: "traducteur", label: "Traducteur", prompt: "You are a professional translator. Preserve meaning, tone, and formatting; output only the target language." },
  { id: "analyste", label: "Analyste", prompt: "You reason step by step and back your claims. Point out assumptions and edge cases." },
  { id: "socratic", label: "Socratique", prompt: "Instead of answering directly, ask guiding questions one at a time so the user reaches the answer themselves." },
];

// Selector value when a free-form (non-persona) prompt is in place: shown as
// a disabled option to name the state, not to be picked.
const CUSTOM = "__custom__";

export function SettingsPanel({
  settings,
  onChange,
  /** Context window of the LOADED model. The output cap adds up on top of
   *  the prompt within it: offering more than the context makes no sense,
   *  and the backend would lower it anyway. The slider therefore follows
   *  the current model. */
  contexte,
  provenance = "manual",
  onProvenance,
}: {
  settings: Settings;
  onChange: (next: Settings) => void;
  contexte?: number;
  provenance?: "persona" | "skill" | "manual";
  onProvenance?: (p: "persona" | "skill" | "manual") => void;
}) {
  const plafond = contexte && contexte > 0 ? contexte : 131072;
  const t = useT();

  const personaActive = PERSONAS.find((p) => p.prompt === settings.system);
  const personaValue = personaActive?.id ?? (settings.system ? CUSTOM : "");

  // Rebuilt at every render (including the stream ones), while it only depends
  // on the language and the selected persona.
  const options = useMemo(() => {
    const o: { value: string; label: string; disabled?: boolean }[] = [
      { value: "", label: t("Aucun — conversation à nu") },
      ...PERSONAS.map((p) => ({ value: p.id, label: t(p.label) })),
    ];
    if (personaValue === CUSTOM) {
      // A free-form prompt is in place: show it as the current state.
      o.push({ value: CUSTOM, label: t("Personnalisé"), disabled: true });
    }
    return o;
  }, [t, personaValue]);

  return (
    <VStack gap={4}>
      <VStack gap={2}>
        <Text type="supporting" color="secondary">{t("Prompt système")}</Text>
        <Selector
          label={t("Persona")}
          size="sm"
          placeholder={t("Aucun — conversation à nu")}
          options={options}
          value={personaValue}
          onChange={(v) => {
            const p = PERSONAS.find((x) => x.id === v);
            onChange({ ...settings, system: p ? p.prompt : "" });
            onProvenance?.(p ? "persona" : "manual");
          }}
        />
        <TextArea
          label={t("System prompt (optionnel)")}
          isLabelHidden
          placeholder={t("Ex : Tu es un assistant concis et technique.")}
          rows={3}
          value={settings.system}
          onChange={(value) => {
            onChange({ ...settings, system: value });
            onProvenance?.("manual");
          }}
        />
        {provenance === "skill" && settings.system ? (
          <Text color="secondary" type="supporting">
            {t("Le prompt système provient d'une compétence. Choisir un persona le remplacera.")}
          </Text>
        ) : null}
      </VStack>
      <VStack gap={2}>
        <Text type="supporting" color="secondary">{t("Génération")}</Text>
        <HStack gap={4} wrap="wrap">
          <Slider
            label={t("Température")}
            min={0}
            max={2}
            step={0.05}
            value={settings.temperature}
            formatValue={(v) => v.toFixed(2)}
            onChange={(value: number | [number, number]) =>
              onChange({ ...settings, temperature: value as number })
            }
          />
          <Slider
            label={t("Max tokens")}
            min={64}
            max={plafond}
            step={256}
            value={Math.min(settings.maxTokens, plafond)}
            onChange={(value: number | [number, number]) =>
              onChange({ ...settings, maxTokens: value as number })
            }
          />
          <Slider
            label={t("Top-p")}
            min={0}
            max={1}
            step={0.05}
            value={settings.topP}
            formatValue={(v) => v.toFixed(2)}
            onChange={(value: number | [number, number]) =>
              onChange({ ...settings, topP: value as number })
            }
          />
        </HStack>
      </VStack>
      <VStack gap={2}>
        <Text type="supporting" color="secondary">{t("Raisonnement")}</Text>
        {/* The label used to say « Afficher le raisonnement »: it is not a
            display but a TRIGGER — the value goes out in
            `chat_template_kwargs.enable_thinking`, so the model starts
            thinking before answering (higher wait time and bill).
            A switch that changes the cost must say so. */}
        <Switch
          label={t("Activer la réflexion du modèle (plus lent, plus coûteux)")}
          value={settings.reasoning}
          onChange={(checked) => onChange({ ...settings, reasoning: checked })}
        />
        {/* Thinking depth (reasoning_effort, sent through the chat
            template). Each model validates its own values — the current
            Qwen3.8 accepts xhigh (its default), medium and low; if the
            template rejects it, the backend retries without. '' = send
            nothing. */}
        <SegmentedControl
          label={t("Effort de raisonnement")}
          value={settings.reasoningEffort || "default"}
          onChange={(v) => onChange({ ...settings, reasoningEffort: v === "default" ? "" : String(v) })}
        >
          <SegmentedControlItem value="default" label={t("Par défaut")} />
          <SegmentedControlItem value="low" label={t("Basse")} />
          <SegmentedControlItem value="medium" label={t("Moyenne")} />
          <SegmentedControlItem value="xhigh" label={t("Maximale")} />
        </SegmentedControl>
      </VStack>
    </VStack>
  );
}
