"use client";

import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { MicrophoneIcon, StopIcon } from "@heroicons/react/24/outline";
import { useEffect, useRef } from "react";
import { useT } from "@/lib/i18n";
import type { Dictation } from "@/lib/useDictation";

/**
 * Dictation button to place next to a text field.
 *
 * The caller provides the object returned by useDictation() rather than the
 * button instantiating it itself: the page needs it on its side to cut the
 * mic (on sending a message, for example), and a hook cannot be called
 * conditionally.
 *
 * Nothing is shown as long as the transcription backend is stopped — better
 * no button than a button that fails.
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
          : error ? t(error) : t("Échec de la transcription."),
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
      // The button doesn't take focus on mouse click: without this, clicking
      // the mic removes focus from the field, and the following Enter re-triggers
      // the button instead of sending the message. preventDefault on mousedown
      // touches neither the click nor keyboard navigation (Tab still gives focus).
      onMouseDown={(e) => e.preventDefault()}
      onClick={toggle}
    />
  );
}
