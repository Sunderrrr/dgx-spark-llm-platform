export type Role = "user" | "assistant";

export type ChatMsg = {
  role: Role;
  content: string;
  reasoning?: string;
  tokens?: number;
  tokensPerSec?: number;
  ttft?: number;
  ts?: number;
  isError?: boolean;
  attachmentCount?: number;
  // Sent to the model but not rendered in the chat (e.g. the answers submitted
  // from a clarifying-question card, which the user doesn't want to see echoed).
  hidden?: boolean;
  /** Réponse coupée net par le plafond de tokens : on le signale et on propose
   *  de reprendre là où le modèle s'est arrêté. */
  truncated?: boolean;
};

export type Attachment = {
  name: string;
  content: string;
};

export type Conversation = {
  /** `client_id` côté serveur : une chaîne, jamais un nombre. */
  id: string;
  title: string;
  ts: number;
  model: string;
  // `hidden` est conservé : une réponse à des questions doit rester cachée
  // après rechargement, sinon les index se décalent et le rendu change.
  messages: { role: Role; content: string; hidden?: boolean }[];
};

export type PlaygroundData = {
  running_models: string[];
  model_limits: Record<string, number>;
  /** Le playground consomme la clé de l'utilisateur : sans clé, rien ne part. */
  has_key: boolean;
};

export type Settings = {
  system: string;
  temperature: number;
  maxTokens: number;
  topP: number;
  reasoning: boolean;
};
