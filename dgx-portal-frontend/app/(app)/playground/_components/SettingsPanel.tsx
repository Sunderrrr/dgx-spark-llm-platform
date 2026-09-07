"use client";

// Panneau des réglages du playground, rendu dans le Popover ancré sur la roue
// crantée : pas de Card ici (le popover fournit la surface — un Card en plus
// ferait un double cadre). Sections titrées pour scander la lecture :
// prompt système (persona + texte), génération, raisonnement.
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Slider } from "@astryxdesign/core/Slider";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { Selector } from "@astryxdesign/core/Selector";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import type { Settings } from "@/lib/types";
import { useT } from "@/lib/i18n";

// Personas : prompts système pré-écrits, choisis dans un menu déroulant plutôt
// qu'en rangée de boutons. Les prompts sont en anglais (instructions système),
// l'UI reste en français.
const PERSONAS: { id: string; label: string; prompt: string }[] = [
  { id: "code", label: "Code", prompt: "You are a senior software engineer. Produce complete, runnable, idiomatic code. Prefer whole files over snippets and never elide parts of a file." },
  { id: "redacteur", label: "Rédacteur", prompt: "You are a careful writer. Be clear, structured, and concise. Favour short paragraphs and useful headings." },
  { id: "traducteur", label: "Traducteur", prompt: "You are a professional translator. Preserve meaning, tone, and formatting; output only the target language." },
  { id: "analyste", label: "Analyste", prompt: "You reason step by step and back your claims. Point out assumptions and edge cases." },
  { id: "socratic", label: "Socratique", prompt: "Instead of answering directly, ask guiding questions one at a time so the user reaches the answer themselves." },
];

// Valeur du sélecteur quand un prompt libre (non persona) est en place : on
// l'affiche comme option désactivée pour nommer l'état, pas pour être choisi.
const CUSTOM = "__custom__";

export function SettingsPanel({
  settings,
  onChange,
  /** Fenêtre de contexte du modèle CHARGÉ. Le plafond de sortie s'y ajoute au
   *  prompt : proposer plus que le contexte n'a aucun sens, et le backend le
   *  rabaisserait de toute façon. Le curseur suit donc le modèle en cours. */
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

  const options: { value: string; label: string; disabled?: boolean }[] = [
    { value: "", label: t("Aucun — conversation à nu") },
    ...PERSONAS.map((p) => ({ value: p.id, label: t(p.label) })),
  ];
  if (personaValue === CUSTOM) {
    // Un prompt libre est en place : on le montre comme état courant.
    options.push({ value: CUSTOM, label: t("Personnalisé"), disabled: true });
  }

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
        <Switch
          label={t("Afficher le raisonnement")}
          value={settings.reasoning}
          onChange={(checked) => onChange({ ...settings, reasoning: checked })}
        />
        {/* Profondeur de réflexion (reasoning_effort, transmis via le chat
            template). Chaque modèle valide ses propres valeurs — le Qwen3.8
            actuel accepte xhigh (son défaut), medium et low ; si le template
            refuse, le backend retente sans. '' = ne rien transmettre. */}
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
