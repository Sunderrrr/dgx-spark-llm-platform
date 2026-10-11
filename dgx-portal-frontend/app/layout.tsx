import type { Metadata } from "next";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
// The motion of the blobatars (the default avatar) lives in a stylesheet:
// without this import, `animate` would do nothing at all and the avatars would
// stay frozen — with no error and no warning. It also carries the
// `prefers-reduced-motion` rule, which stops them.
import "blobatar/motion.css";
import "./globals.css";
import { cookies } from "next/headers";
import { ThemeProvider, type Mode } from "./theme-provider";
import { MODE_COOKIE } from "@/lib/theme-cookie";
import { WhoamiProvider, type Whoami } from "@/lib/whoami";
import { CsrfProvider } from "@/lib/csrf";
import { BrandingProvider } from "@/lib/branding";
import { brandingFromConfig } from "@/lib/branding-shape";
import { serverRead } from "@/lib/server-api";
import type { ThemeId } from "@/lib/themes";

export const metadata: Metadata = {
  title: "DGX",
  description: "Plateforme IA privée — NVIDIA DGX Spark.",
};

// Required for nonce-based CSP (see proxy.ts): Next.js only injects a nonce
// into its own framework scripts during server-side rendering, based on the
// CSP header on the incoming request. A statically-prerendered page is built
// once at build time with no request/nonce available, so every page here
// must render per-request instead.
export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Identity and instance branding, read on the server in parallel: the shell
  // is sent with the right name, avatar, admin entries, palette and language,
  // instead of defaults the client corrected one round-trip later. The CSRF
  // token is NOT read here: /api/csrf may create it, and a session cookie set
  // on this server-side request would never reach the browser.
  const [whoRead, configRead, jar] = await Promise.all([
    serverRead<Whoami>("/api/whoami"),
    serverRead<{ branding?: { name?: string; logo?: string | null } }>("/api/config"),
    cookies(),
  ]);
  // undefined = the read failed: the provider then fetches it in the browser.
  const who =
    whoRead.status === "ok" ? whoRead.data : whoRead.status === "unauthenticated" ? null : undefined;
  const lang = who?.lang === "en" ? "en" : "fr";
  const cookieMode = jar.get(MODE_COOKIE)?.value;
  const mode: Mode = cookieMode === "light" || cookieMode === "dark" ? cookieMode : "system";
  return (
    <html lang={lang} suppressHydrationWarning>
      <body>
        <WhoamiProvider initial={who}>
          <CsrfProvider>
            <BrandingProvider initial={configRead.status === "ok" ? brandingFromConfig(configRead.data) : null}>
              <ThemeProvider
                initial={{ mode, themeId: (who?.theme_id || "neutral") as ThemeId, lang }}
              >
                {children}
              </ThemeProvider>
            </BrandingProvider>
          </CsrfProvider>
        </WhoamiProvider>
      </body>
    </html>
  );
}
