/**
 * The English dictionary, assembled from one module per domain.
 *
 * The French text is the key (like a gettext msgid) and the English text the
 * value: the app is written in French, so French mode is the identity and a
 * missing entry falls back to French instead of showing a key name.
 *
 * The modules are spread in the order their entries were appended to the
 * historical single-file dictionary. That order matters in one place: the
 * `tServer` template table sorts by key length, and two templates of equal
 * length keep their insertion order — so keep this list in source order when
 * adding a module, and never rename a key.
 *
 * `EN` is internal to the i18n folder: the public surface is `lib/i18n.tsx`
 * (a thin re-export) and `lib/i18n/index.ts`.
 */
import { dictAdminConfig } from "./dict-admin-config";
import { dictAdmin } from "./dict-admin";
import { dictApiKeys } from "./dict-api-keys";
import { dictAppendix } from "./dict-appendix";
import { dictHome } from "./dict-home";
import { dictMedia } from "./dict-media";
import { dictMessages } from "./dict-messages";
import { dictModels } from "./dict-models";
import { dictPlayground } from "./dict-playground";
import { dictSecurity } from "./dict-security";
import { dictServer } from "./dict-server";
import { dictSettings } from "./dict-settings";
import { dictShell } from "./dict-shell";
import { dictSupport } from "./dict-support";

export const EN: Record<string, string> = {
  ...dictModels,
  ...dictMessages,
  ...dictShell,
  ...dictAdminConfig,
  ...dictSettings,
  ...dictHome,
  ...dictApiKeys,
  ...dictPlayground,
  ...dictSupport,
  ...dictAdmin,
  ...dictMedia,
  ...dictAppendix,
  ...dictSecurity,
  ...dictServer,
};
