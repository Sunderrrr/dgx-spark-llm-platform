"use client";

import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Slider } from "@astryxdesign/core/Slider";
import { Switch } from "@astryxdesign/core/Switch";
import type { Settings } from "@/lib/types";
import { useT } from "@/lib/i18n";

export function SettingsPanel({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (next: Settings) => void;
}) {
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
            max={131072}
            step={256}
            value={settings.maxTokens}
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
