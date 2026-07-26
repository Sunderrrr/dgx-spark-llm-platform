import type { Metadata } from "next";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./globals.css";
import { ThemeProvider } from "./theme-provider";

export const metadata: Metadata = {
  title: "Cronos",
  description: "Plateforme IA privée — NVIDIA DGX Spark.",
};

// Required for nonce-based CSP (see proxy.ts): Next.js only injects a nonce
// into its own framework scripts during server-side rendering, based on the
// CSP header on the incoming request. A statically-prerendered page is built
// once at build time with no request/nonce available, so every page here
// must render per-request instead.
export const dynamic = "force-dynamic";

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="fr" suppressHydrationWarning>
      <body>
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
