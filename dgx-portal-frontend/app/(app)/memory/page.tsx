"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useSettingsDialog } from "@/lib/settings-dialog";

/**
 * Memory no longer has a page: it became a settings tab. This
 * route only survives for links already opened or bookmarked — without it,
 * `/memory` is no longer served by Next, the request goes to the Flask
 * backend and the user lands on a raw « Not Found » instead of the
 * application. We send back home while directly opening the right tab.
 */
export default function MemoryRedirect() {
  const router = useRouter();
  const { open } = useSettingsDialog();

  useEffect(() => {
    router.replace("/");
    open("memory");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once only, on mount
  }, []);

  return null;
}
