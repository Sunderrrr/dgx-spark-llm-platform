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
  /** Attached images (`data:` URLs), sent to the model if it can read them. */
  images?: string[];
  // Sent to the model but not rendered in the chat (e.g. the answers submitted
  // from a clarifying-question card, which the user doesn't want to see echoed).
  hidden?: boolean;
  /** Response cut short by the token ceiling: we flag it and offer
   *  to resume where the model stopped. */
  truncated?: boolean;
};

export type Attachment = {
  name: string;
  content: string;
  /** IMAGE attachment: already-downscaled `data:` URL, `content` stays empty. */
  image?: string;
};

export type Conversation = {
  /** `client_id` on the server side: a string, never a number. */
  id: string;
  title: string;
  ts: number;
  model: string;
  // `hidden` is kept: an answer to questions must stay hidden
  // after reload, otherwise the indexes shift and the rendering changes.
  messages: {
    role: Role;
    content: string;
    hidden?: boolean;
    // Kept server-side, so useful for display after reload:
    // without them, a truncated answer looked complete again (see persist()).
    isError?: boolean;
    truncated?: boolean;
  }[];
  // BOUNDED history list: beyond a byte budget, the server only
  // carries the metadata and marks the conversation. It reloads
  // item by item on opening (`fetchConversation`) — without this flag, a
  // heavy conversation would open on an empty thread.
  messagesOmis?: boolean;
};

export type PlaygroundData = {
  running_models: string[];
  model_limits: Record<string, number>;
  /** Models that read images (read from their launch arguments). */
  model_vision?: Record<string, boolean>;
  /** The playground uses the user's key: without a key, nothing goes out. */
  has_key: boolean;
};

export type Settings = {
  system: string;
  temperature: number;
  maxTokens: number;
  topP: number;
  reasoning: boolean;
  /** Reasoning depth (chat_template_kwargs.reasoning_effort). '' = the
   *  model template's default value — the accepted values depend
   *  on the model: the backend retries without it if the template refuses. */
  reasoningEffort: string;
};
