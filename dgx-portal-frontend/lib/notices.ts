/** Notices système du serveur (`cronos_notice`) : msgid français + arguments.
 *
 * Traduites au rendu dans la langue de l'interface — le contrat i18n
 * (français = clé) s'applique comme pour le reste de l'UI ; le serveur
 * n'écrit jamais de phrase, donc pas de langue côté backend.
 *
 * Partagé par le playground et l'assistant Support : les deux tournent sur la
 * clé API de l'utilisateur, donc sur son budget, et peuvent recevoir les mêmes
 * refus (aucune clé créée, quota épuisé).
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
    default:
      return notice.id;
  }
}
