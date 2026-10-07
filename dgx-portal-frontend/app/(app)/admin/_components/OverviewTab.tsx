"use client";

// Vue d'ensemble — what the operator looks at ten times a day: state only,
// nothing to act on. Anything with a button lives on the other tabs
// (ONE ACTION = ONE TAB).

import { VStack } from "@astryxdesign/core/Stack";
import { PlatformStatus, type PlatformStatusData } from "./PlatformStatus";
import { LiveActivity } from "./LiveActivity";

export function OverviewTab({ platform }: { platform: PlatformStatusData | null }) {
  return (
    <VStack gap={6}>
      {/* Dashboard widget (disk, backup, monitor, served model, counters):
          refreshed by the same 8 s poll as the rest of the page — the data
          flows down, no second timer. */}
      <PlatformStatus status={platform} />
      <LiveActivity />
    </VStack>
  );
}
