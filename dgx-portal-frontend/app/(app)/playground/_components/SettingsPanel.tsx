"use client";

import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Slider } from "@astryxdesign/core/Slider";
import { Switch } from "@astryxdesign/core/Switch";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import type { Settings } from "@/lib/types";
import { useT } from "@/lib/i18n";

// Presets de prompt système (personas) : appliqués en un clic au champ system.
// Les prompts sont en anglais (instructions système), l'UI reste en français.
const PERSONAS: { id: string; label: string; prompt: string }[] = [
  { id: "code", label: "Code", prompt: "You are a senior software engineer. Produce complete, runnable, idiomatic code. Prefer whole files over snippets and never elide parts of a file." },
  { id: "redacteur", label: "Rédacteur", prompt: "You are a careful writer. Be clear, structured and concise. Favour short paragraphs and useful headings." },
  { id: "traducteur", label: "Traducteur", prompt: "You are a professional translator. Preserve meaning, tone and formatting; output only the target language." },
  { id: "analyste", label: "Analyste", prompt: "You reason step by step and back your claims. Point out assumptions and edge cases." },
  { id: "socratic", label: "Socratique", prompt: "Instead of answering directly, ask guiding questions one at a time so the user reaches the answer themselves." },
];

export function SettingsPanel({
  settings,
  onChange,
  /** Fenêtre de contexte du modèle CHARGÉ. Le plafond de sortie s'y ajoute au
   *  prompt : proposer plus que le contexte n'a aucun sens, et le backend le
   *  rabaisserait de toute façon. Le curseur suit donc le modèle en cours. */
  contexte,
}: {
  settings: Settings;
  onChange: (next: Settings) => void;
  contexte?: number;
}) {
  const plafond = contexte && contexte > 0 ? contexte : 131072;
  const t = useT();
  return (
    <Card>
      <VStack gap={4}>
        <TextArea
          label={t("System prompt (optionnel)")}
          placeholder={t("Ex : Tu es un assistant concis et technique.")}
          rows={2}
          value={settings.system}
          onChange={(value) => onChange({ ...settings, system: value })}
        />
        <VStack gap={1}>
          <Text type="supporting" color="secondary">{t("Personas")}</Text>
          <HStack gap={2} vAlign="center" wrap="wrap">
            {PERSONAS.map((p) => (
              <Button
                key={p.id}
                label={t(p.label)}
                variant={settings.system === p.prompt ? "secondary" : "ghost"}
                size="sm"
                onClick={() => onChange({ ...settings, system: p.prompt })}
              />
            ))}
            {settings.system && (
              <Button
                label={t("Effacer le system prompt")}
                variant="ghost"
                size="sm"
                onClick={() => onChange({ ...settings, system: "" })}
              />
            )}
          </HStack>
        </VStack>
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
        <Switch
          label={t("Afficher le raisonnement")}
          value={settings.reasoning}
          onChange={(checked) => onChange({ ...settings, reasoning: checked })}
        />
      </VStack>
    </Card>
  );
}
