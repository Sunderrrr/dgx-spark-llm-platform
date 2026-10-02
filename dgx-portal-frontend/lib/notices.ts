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
export type CronosNotice = { id: string; reset?: string; status?: number };

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
    case "model_error":
      return t("Erreur modèle ({status}).").replace("{status}", String(notice.status ?? ""));
    case "image_service_off":
      return t("La génération d'image est indisponible pour l'instant : le service est arrêté. Un admin peut le démarrer depuis l'espace Admin ; le modèle répondra sans image.");
    default:
      return notice.id;
  }
}
