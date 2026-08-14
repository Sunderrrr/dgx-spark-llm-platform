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
};

export type Attachment = {
  name: string;
  content: string;
};

export type Conversation = {
  id: number;
  title: string;
  ts: number;
  model: string;
  messages: { role: Role; content: string }[];
};

export type PlaygroundData = {
  running_models: string[];
  model_limits: Record<string, number>;
};

export type Settings = {
  system: string;
  temperature: number;
  maxTokens: number;
  topP: number;
  reasoning: boolean;
};
