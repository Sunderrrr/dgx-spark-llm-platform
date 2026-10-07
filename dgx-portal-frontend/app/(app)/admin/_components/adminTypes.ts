// Shared types and vocabulary of the admin screen.
//
// The admin page is cut into five tabs (Vue d'ensemble / Modèles /
// Utilisateurs / Demandes / Système); the data layer (fetch loop, SSE stream,
// `act`) stays in page.tsx and flows down to the tab components. These types
// are shared by several tabs, hence their own module: importing them from
// page.tsx would make the tabs depend on the page that renders them.

export type ModelCfg = { id: number; name: string; hf_model_id: string; engine: string; vllm_args: string };
export type OcrCfg = { id: number; name: string; hf_model_id: string; vllm_args: string };
export type VoiceCfg = { id: number; name: string; repo_id: string };
export type ModelRequest = { id: number; fullname: string; username: string; model_id: string; reason: string | null; status: string; created_at: string };
export type BudgetRequest = {
  id: number;
  fullname: string;
  username: string;
  key_alias: string;
  current_budget: number | null;
  reason: string | null;
  status: string;
  created_at: string;
  granted_amount: number | null;
};
export type BudgetGrant = { username: string; base_budget: number; current_budget: number; expires_at: string };
export type SpendRow = { username: string; tokens: number; max_budget: number | null; unlimited: boolean; key_count: number };
export type AuditRow = { id: number; username: string; action: string; detail: string; created_at: string };
export type UsageRow = { username: string; c: number; last: string };
export type VStatus = { status: string; model: string | null; pid: number | null; engine?: string };

export type AdminData = {
  requests: ModelRequest[];
  running_models: string[];
  stats: { pending: number; done: number; rejected: number; budget_pending: number };
  spend_data: SpendRow[];
  ocr_usage: UsageRow[];
  video_usage: UsageRow[];
  voice_usage: UsageRow[];
  model_cfgs: ModelCfg[];
  v_status: VStatus;
  init_logs: string[];
  budget_reqs: BudgetRequest[];
  budget_grants: BudgetGrant[];
  default_key_budget: number;
  default_key_duration: string;
  maintenance_mode: boolean;
  ocr_status: string;
  ocr_model_name: string | null;
  video_status: string;
  voice_status: string;
  voice_model_name: string | null;
  asr_status: string;
  /** Added in parallel on the portal side; absent from the old backend — the
      row is simply not displayed while the field is missing. */
  asr_model_name?: string | null;
  /** Cause of the dictation model load failure (« CUDA error: out of
      memory », measured on 2026-10-01), published by the sidecar. Absent until
      a load has failed. */
  asr_load_error?: string | null;
  image_status: string;
  image_model_name: string | null;
  image_model_ids: string[];
  music_status: string;
  music_model_name: string | null;
  ocr_cfgs: OcrCfg[];
  voice_cfgs: VoiceCfg[];
};

export type CatalogKind = "llm" | "ocr" | "voice" | "video" | "image" | "music";

/** Result of an admin action, as the backend now answers it:
 * {ok: false, error} on refusal, {ok: true, message?, warning?} otherwise. */
export type ActResult = { ok: boolean; error?: string; warning?: string };

/** A POST to an admin action route: it toasts the verdict and refreshes the
 *  page data (see act() in page.tsx). */
export type ActFn = (url: string, params?: Record<string, string>) => Promise<ActResult>;

/** The four destructive/impactful actions that require an explicit confirmation
 *  before the POST (cf. ConfirmActionDialog). */
export type ConfirmAction =
  | { kind: "stop-model" }
  | { kind: "launch"; name: string }
  | { kind: "delete-model"; id: number; name: string }
  | { kind: "maintenance" };

/** What every tab receives: the polled data plus the single action entry
 *  point. The tabs hold no fetch loop of their own (except the live-activity
 *  card), so the page keeps ONE 8 s poll and ONE in-flight lock. */
export type AdminTabProps = {
  data: AdminData | null;
  act: ActFn;
  actionDisabled: boolean;
  setConfirmAction: (action: ConfirmAction | null) => void;
};

export const SIDECAR_VARIANT: Record<string, "success" | "warning" | "neutral" | "error"> = {
  running: "success",
  starting: "warning",
  // The container runs but its model could not load: it is neither
  // « en ligne » nor « en cours de démarrage », and the wait never ends.
  failed: "error",
  stopped: "neutral",
};
export const SIDECAR_LABEL: Record<string, string> = {
  running: "En ligne",
  starting: "Démarrage…",
  failed: "Échec du chargement",
  stopped: "Arrêté",
};

export const REQ_STATUS_VARIANT: Record<string, "warning" | "success" | "error"> = { pending: "warning", done: "success", rejected: "error" };
export const REQ_STATUS_LABEL: Record<string, string> = { pending: "En attente", done: "Lancé ✓", rejected: "Refusé" };

/** « 10M » rather than « 10 000 000 » on the buttons: readable at a glance.
    Amounts that do not round cleanly stay spelled out. */
export const fmtCompact = (n: number, numLocale: string) =>
  n >= 1_000_000 && n % 1_000_000 === 0 ? `${Math.round(n / 1_000_000)}M` : Math.round(n).toLocaleString(numLocale);
