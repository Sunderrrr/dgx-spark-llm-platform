"use client";
// Run the FULL validation pipeline from the admin: code gates, running
// service, user journeys, health — the same `scripts/valider.sh` a developer
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

type Etat = {
  en_cours: boolean;
  duree_s: number;
  code: number | null;
  sortie: string[];
  reussi: boolean | null;
};

export function ValidationPanel({ actionDisabled }: { actionDisabled: boolean }) {
  const t = useT();
  const csrf = useCsrf();
  const [etat, setEtat] = useState<Etat | null>(null);
  // The journal is APPENDED to, never swapped: a CodeBlock re-rendered from
  // scratch every 2 s reads as a flicker (« les logs apparaissent et
  // disparaissent »). Keep the rendered lines and add only the new ones.
  const rendu = useRef<string[]>([]);
  const [erreur, setErreur] = useState<string | null>(null);
  const minuterie = useRef<ReturnType<typeof setInterval> | null>(null);

  async function lire() {
    try {
      const r = await fetch("/admin/valider/etat");
      if (r.ok) {
        const d = await r.json();
        setEtat(d);
      }
    } catch {
      /* the next poll will retry */
    }
  }

  useEffect(() => {
    return () => {
      if (minuterie.current) clearInterval(minuterie.current);
    };
  }, []);

  async function lancer() {
    setErreur(null);
    const r = await fetch("/admin/valider", {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
      body: "go=1",
    });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) {
      setErreur(d.error || t("Échec du lancement"));
      return;
    }
    if (minuterie.current) clearInterval(minuterie.current);
    minuterie.current = setInterval(lire, 2000);
    lire();
  }

  const encours = etat?.en_cours === true;
  const verdict =
    etat?.reussi === true ? "success" : etat?.reussi === false ? "error" : "neutral";

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
            {etat && etat.reussi !== null ? (
              <Badge
                label={etat.reussi ? t("Tout est vert") : t("Un contrôle échoue")}
                variant={verdict as "success" | "error" | "neutral"}
              />
            ) : null}
            {encours ? (
              <Text type="supporting" color="secondary">
                {t("En cours depuis {n} s").replace("{n}", String(etat?.duree_s ?? 0))}
              </Text>
            ) : null}
            <Button
              label={encours ? t("En cours…") : t("Lancer la vérification")}
              variant={encours ? "secondary" : "primary"}
              icon={<Icon icon={encours ? ArrowPathIcon : PlayIcon} size="sm" />}
              isDisabled={actionDisabled || encours}
              onClick={lancer}
            />
          </HStack>
        </HStack>
        {erreur ? <Text color="secondary">{erreur}</Text> : null}
        {/* The failing line is the product: a red dot tells you WHERE to look,
            the last lines tell you WHAT happened. */}
        {(() => {
          // Append-only: never re-render the whole journal, only grow it.
          if (etat && etat.sortie.length > rendu.current.length) {
            rendu.current = etat.sortie;
          }
          return rendu.current.length > 0 ? (
            <CodeBlock code={rendu.current.join("\n")} maxHeight={280} />
          ) : null;
        })()}
      </VStack>
    </Card>
  );
}
