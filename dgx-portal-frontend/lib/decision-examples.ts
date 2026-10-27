/**
 * The starting points offered by « Voir des exemples de requêtes ».
 *
 * Each one is a complete request: the state and the questions, as the two
 * editors take them. The image example exists because the reference offers
 * one — this model is TEXT ONLY, so the page says so instead of pretending.
 */

export type ExampleKind = "text" | "image";

export type DecisionExample = {
  id: string;
  /** Short name, the row's title. */
  title: string;
  /** One line saying what the model is asked to decide. */
  description: string;
  kind: ExampleKind;
  state: string;
  questions: string;
};

const RECLAMATION: DecisionExample = {
  id: "reclamation",
  title: "Réclamation client",
  description: "Facturé deux fois : remboursement, équipe, urgence.",
  kind: "text",
  state: `{
  "commande": { "id": 4411, "montant": 89.9, "debite_deux_fois": true },
  "message": "Ma commande 4411 a été débitée deux fois, personne ne répond depuis une semaine et je veux un remboursement immédiat."
}`,
  questions: `{
  "remboursement": {
    "type": "noul",
    "instructions": "Le client demande-t-il un remboursement ?"
  },
  "equipe": {
    "type": "choice",
    "instructions": "Quelle équipe doit traiter ce message ?",
    "criteria": { "facturation": null, "livraison": null, "technique": null }
  },
  "urgence": {
    "type": "score",
    "instructions": "À quel point ce message est-il urgent ?",
    "criteria": ["peut attendre", "cette semaine", "dans la journée", "tout de suite"]
  }
}`,
};

const TICKET: DecisionExample = {
  id: "ticket",
  title: "Ticket de support",
  description: "Trier un incident : gravité, catégorie, blocage.",
  kind: "text",
  state: `{
  "ticket": { "id": "SUP-2044", "ouvert_depuis_h": 30, "client": "abonnement pro" },
  "message": "Depuis la mise à jour d'hier, notre export automatique échoue toutes les nuits sur le fichier de 400 Mo. L'équipe compta ne peut plus clôturer le mois. Personne n'a répondu au dernier ticket."
}`,
  questions: `{
  "gravite": {
    "type": "score",
    "instructions": "Quelle est la gravité de l'incident ?",
    "criteria": ["confort", "gênant", "bloquant", "arrêt total"]
  },
  "categorie": {
    "type": "choice",
    "instructions": "Dans quel domaine classer l'incident ?",
    "criteria": { "export": null, "authentification": null, "facturation": null, "interface": null }
  },
  "blocage": {
    "type": "noul",
    "instructions": "Le client est-il empêché de travailler ?"
  }
}`,
};

const PHOTO: DecisionExample = {
  id: "photo-produit",
  title: "Photo de produit",
  description: "Contrôle qualité sur une photo — image, non lue par ce modèle.",
  kind: "image",
  state: `{
  "image": {
    "url": "https://exemple.invalid/photos/casque-4411.jpg",
    "alt": "casque audio noir posé sur une table, coque fissurée sur le côté droit",
    "poids_ko": 240
  },
  "note": "Photo envoyée par le client après réception."
}`,
  questions: `{
  "conforme": {
    "type": "noul",
    "instructions": "Le produit semble-t-il conforme à la commande ?"
  },
  "defaut": {
    "type": "choice",
    "instructions": "Quel défaut est visible ?",
    "criteria": { "aucun": null, "fissure": null, "rayure": null, "manquant": null }
  },
  "gravite": {
    "type": "score",
    "instructions": "Quelle est la gravité du défaut ?",
    "criteria": ["esthétique", "gênant", "inutilisable"]
  }
}`,
};

export const EXAMPLES: DecisionExample[] = [RECLAMATION, TICKET, PHOTO];

/** The example loaded when the page offers a first request. */
export const DEFAULT_EXAMPLE = RECLAMATION;
