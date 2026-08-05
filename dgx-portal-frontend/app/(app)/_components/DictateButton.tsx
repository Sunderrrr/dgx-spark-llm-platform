"use client";

import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { MicrophoneIcon, StopIcon } from "@heroicons/react/24/outline";
import { useEffect, useRef } from "react";
import { useT } from "@/lib/i18n";
import type { Dictation } from "@/lib/useDictation";

/**
 * Bouton de dictée à poser à côté d'un champ texte.
 *
 * L'appelant fournit l'objet renvoyé par useDictation() plutôt que le bouton
 * de l'instancier lui-même : la page en a besoin de son côté pour couper le
 * micro (à l'envoi d'un message, par exemple), et un hook ne peut pas être
 * appelé conditionnellement.
 *
 * Rien ne s'affiche tant que le backend de transcription est arrêté — mieux
 * vaut pas de bouton qu'un bouton qui échoue.
 */
export function DictateButton({
  dictation,
  isDisabled,
  size = "sm",
}: {
  dictation: Dictation;
  isDisabled?: boolean;
  size?: "sm" | "md";
}) {
  const t = useT();
  const showToast = useToast();
  const { available, isRecording, isTranscribing, error, toggle } = dictation;
  const seen = useRef<string | null>(null);

  useEffect(() => {
    if (error === null || error === seen.current) return;
    seen.current = error;
    showToast({
      body:
        error === "mic"
          ? t("Micro inaccessible — autorise l'accès au microphone dans ton navigateur.")
          : error || t("Échec de la transcription."),
      type: "error",
    });
  }, [error, showToast, t]);

  if (!available) return null;

  return (
    <Button
      label={
        isRecording ? t("Arrêter la dictée")
        : isTranscribing ? t("Transcription…")
        : t("Dicter")
      }
      variant={isRecording ? "primary" : "ghost"}
      size={size}
      isIconOnly
      isLoading={isTranscribing}
      isDisabled={isDisabled}
      icon={<Icon icon={isRecording ? StopIcon : MicrophoneIcon} size="sm" />}
      // Le bouton ne prend pas le focus à la souris : sans ça, cliquer sur le
      // micro le retire du champ, et l'Entrée qui suit ré-actionne le bouton
      // au lieu d'envoyer le message. preventDefault sur mousedown ne touche
      // ni au clic ni au parcours clavier (Tab donne toujours le focus).
      onMouseDown={(e) => e.preventDefault()}
      onClick={toggle}
    />
  );
}
