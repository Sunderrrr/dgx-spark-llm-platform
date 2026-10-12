"use client";
// Run the FULL validation pipeline from the admin: code gates, running
// service, user journeys, health — the same `scripts/validate.sh` a developer
// runs before pushing, on demand.
//
// « je puisse lancer un test qui teste tout et me dise si tout va bien ou là où
// ça n'a pas marché » — the value is the « où ça n'a pas marché »: the panel
// shows the failing line, not only a red dot. It takes minutes, so it polls a
// state route instead of holding a request open, and it says how long it has
// been running — silence and « 500 » are the two ways a long job loses trust.

import { useEffect, useRef, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Icon } from "@astryxdesign/core/Icon";
import { PlayIcon, ArrowPathIcon } from "@heroicons/react/24/outline";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { useCsrf } from "@/lib/useCsrf";
import { useT } from "@/lib/i18n";

type State = {
  running: boolean;
  duration_s: number;
  code: number | null;
  output: string[];
  passed: boolean | null;
};

export function ValidationPanel({ actionDisabled }: { actionDisabled: boolean }) {
  const t = useT();
  const csrf = useCsrf();
  const [state, setState] = useState<State | null>(null);
  // The journal only ever GROWS: a CodeBlock re-rendered from scratch every 2 s
  // reads as a flicker (« les logs apparaissent et disparaissent »). State, not
  // a ref — a ref is not reactive and React forbids reading one at render.
  const [rendered, setRendered] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  async function readValue() {
    try {
      const r = await fetch("/admin/valider/etat");
      if (r.ok) {
        const d = await r.json();
        setState(d);
        setRendered((before) => (d.output && d.output.length > before.length ? d.output : before));
      }
    } catch {
      /* the next poll will retry */
    }
  }

  useEffect(() => {
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  async function launch() {
    setError(null);
    const r = await fetch("/admin/valider", {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
      body: "go=1",
    });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) {
      setError(d.error || t("Échec du lancement"));
      return;
    }
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(readValue, 2000);
    readValue();
  }

  const inProgress = state?.running === true;
  const verdict =
    state?.passed === true ? "success" : state?.passed === false ? "error" : "neutral";

  return (
    <Card>
      <VStack gap={3}>
        <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Vérification complète")}</Text>
            <Text type="supporting" color="secondary">
              {t("Code, service, parcours utilisateur, santé — comme avant un push.")}
            </Text>
          </VStack>
          <HStack gap={2} vAlign="center">
            {state && state.passed !== null ? (
              <Badge
                label={state.passed ? t("Tout est vert") : t("Un contrôle échoue")}
                variant={verdict as "success" | "error" | "neutral"}
              />
            ) : null}
            {inProgress ? (
              <Text type="supporting" color="secondary">
                {t("En cours depuis {n} s").replace("{n}", String(state?.duration_s ?? 0))}
              </Text>
            ) : null}
            <Button
              label={inProgress ? t("En cours…") : t("Lancer la vérification")}
              variant={inProgress ? "secondary" : "primary"}
              icon={<Icon icon={inProgress ? ArrowPathIcon : PlayIcon} size="sm" />}
              isDisabled={actionDisabled || inProgress}
              onClick={launch}
            />
          </HStack>
        </HStack>
        {error ? <Text color="secondary">{error}</Text> : null}
        {/* The failing line is the product: a red dot tells you WHERE to look,
            the last lines tell you WHAT happened. */}
        {rendered.length > 0 ? (
          <CodeBlock code={rendered.join("\n")} maxHeight={280} />
        ) : null}
      </VStack>
    </Card>
  );
}
