"use client";

import { useEffect, useState } from "react";

/** True on phone-width viewports. Used to collapse side-by-side layouts (Settings
 *  rail, OCR two columns, the playground artifact panel…) into a stacked/overlay
 *  form so nothing gets squeezed off-screen on mobile. SSR-safe: starts false
 *  (desktop) and corrects on mount. */
export function useIsNarrow(maxWidth = 820): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${maxWidth}px)`);
    const update = () => setNarrow(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [maxWidth]);
  return narrow;
}
