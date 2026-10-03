/** Server system notices (`cronos_notice`): French msgid + arguments.
 *
 * Translated at render time into the UI language — the i18n contract
 * (French = key) applies as for the rest of the UI; the server
 * never writes a sentence, so no language on the backend side.
 *
 * Shared by the playground and the Support assistant: both run on the
 * user's API key, hence on their budget, and can receive the same
 * refusals (no key created, quota exhausted).
 */
export type CronosNotice = {
  id: string;
  reset?: string;
  status?: number;
  /** Seconds to wait, sent with `chat_rate_limited`. */
  wait?: number;
  /** Free memory in GiB (MemAvailable), sent with `image_service_off`. */
  libre_gib?: number;
};

export function texteNotice(
  notice: CronosNotice,
  t: (fr: string) => string,
): string {
  switch (notice.id) {
    case "quota_exceeded": {
      let texte = t("Quota dépassé : tu as épuisé ton budget de tokens pour la période en cours.");
      if (notice.reset) texte += " " + t("Nouveau quota le {date} (UTC).").replace("{date}", notice.reset);
      return texte + " " + t("Tu peux demander plus à l'admin (accueil → « Demander plus de budget »).");
    }
    case "no_api_key":
      return t("Crée d'abord une clé API (page Mes clés API) : cet assistant consomme le budget de ton compte.");
    case "model_error":   // legacy alias — the portal emits only model_replied_error
      return t("Le modèle a renvoyé une erreur ({status}). Réessaie.").replace("{status}", String(notice.status ?? ""));
    // Chat stream notices (2026-10-02): the SSE error texts of the playground
    // and the Support chat became structured notices — the server writes no
    // sentence, this file is where the sentence lives.
    case "empty_message":
      return t("Aucun message à envoyer.");
    case "chat_rate_limited":
      return t("Trop de messages d'affilée — réessaie dans {wait} s.").replace("{wait}", String(notice.wait ?? "?"));
    case "no_model_running":
      return t("Aucun modèle n'est actif sur le serveur.");
    case "model_unreachable":
      return t("Le service de modèle est momentanément injoignable. Réessaie dans un instant.");
    case "model_replied_error":
      return t("Le modèle a renvoyé une erreur ({status}). Réessaie.").replace("{status}", String(notice.status ?? ""));
    case "empty_reply":
      return t("(réponse vide)");
    case "model_busy":
      return t("Le modèle est occupé, réessaie dans un instant.");
    case "reformulate":
      return t("Peux-tu reformuler ta demande ?");
    case "model_timeout":
      return t("Le modèle n'a pas répondu à temps. Réessaie dans un instant.");
    case "image_service_off": {
      let texte = t("La génération d'image est indisponible pour l'instant : le service est arrêté. Un admin peut le démarrer depuis l'espace Admin ; le modèle répondra sans image.");
      // Free memory: under a ~100 GB model the sidecar often simply cannot
      // start — saying only "the service is stopped" makes a click look enough.
      if (notice.libre_gib != null) {
        texte += " " + t("Mémoire disponible : {n} Gio.").replace("{n}", String(notice.libre_gib));
      }
      return texte;
    }
    default:
      return notice.id;
  }
}
