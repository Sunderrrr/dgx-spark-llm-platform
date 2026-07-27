"use client";

import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { KeysContent } from "./_components/KeysContent";

export default function KeysPage() {
  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <KeysContent />
        </LayoutContent>
      }
    />
  );
}
