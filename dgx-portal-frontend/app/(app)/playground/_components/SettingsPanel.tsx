"use client";

import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Slider } from "@astryxdesign/core/Slider";
import { Switch } from "@astryxdesign/core/Switch";
import type { Settings } from "@/lib/types";

export function SettingsPanel({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (next: Settings) => void;
}) {
  return (
    <Card>
      <VStack gap={4}>
        <TextArea
          label="System prompt (optionnel)"
          placeholder="Ex : Tu es un assistant concis et technique."
          rows={2}
          value={settings.system}
          onChange={(value) => onChange({ ...settings, system: value })}
        />
        <HStack gap={4} wrap="wrap">
          <Slider
            label="Température"
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
            label="Max tokens"
            min={64}
            max={131072}
            step={256}
            value={settings.maxTokens}
            onChange={(value: number | [number, number]) =>
              onChange({ ...settings, maxTokens: value as number })
            }
          />
          <Slider
            label="Top-p"
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
          label="Afficher le raisonnement"
          value={settings.reasoning}
          onChange={(checked) => onChange({ ...settings, reasoning: checked })}
        />
      </VStack>
    </Card>
  );
}
