"use client";

import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { useT } from "@/lib/i18n";

export function fmtK(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}k` : `${n}`;
}

export function ContextMeter({ used, max }: { used: number; max: number }) {
  const t = useT();
  const variant = used / max >= 0.95 ? "error" : used / max >= 0.8 ? "warning" : "accent";
  return (
    <ProgressBar
      label={t("Utilisation du contexte")}
      value={used}
      max={max}
      variant={variant}
      isLabelHidden
      hasValueLabel
      formatValueLabel={() => `${fmtK(used)} / ${fmtK(max)} tokens`}
    />
  );
}
