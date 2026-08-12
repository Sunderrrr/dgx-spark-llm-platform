import { useEffect, useRef, useState } from "react";

// Keep a scroll container pinned to the bottom while content streams in, and
// expose a "jump to bottom" affordance when the user scrolls away.
//
//  - `dep`    : the streaming content (or any value that changes as new content
//               arrives). When it changes and the user is still "stuck" to the
//               bottom, the container is scrolled down to follow.
//  - `active` : whether auto-follow should run (e.g. only while streaming). When
//               false the container is never force-scrolled — the user reads at
//               their own pace — but the jump button still works.
//
// Returns a callback ref to attach to the scroll container (a callback rather
// than a ref object so it satisfies both HTMLElement and HTMLDivElement targets
// and doesn't trip the "no ref access during render" rule), an `onScroll`
// handler, whether the jump button should show, and a `scrollToBottom` action.
export function useStickToBottom(dep: unknown, active: boolean) {
  const el = useRef<HTMLElement | null>(null);
  const stuck = useRef(true);
  const [showButton, setShowButton] = useState(false);

  // Distance below which the view counts as "at the bottom" (tolerates
  // sub-pixel rounding and the last line still being laid out mid-stream).
  const THRESHOLD = 48;

  const measure = () => {
    const node = el.current;
    if (!node) return;
    const dist = node.scrollHeight - node.scrollTop - node.clientHeight;
    stuck.current = dist <= THRESHOLD;
    setShowButton(dist > THRESHOLD);
  };

  const scrollToBottom = () => {
    const node = el.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    stuck.current = true;
    setShowButton(false);
  };

  const setRef = (node: HTMLElement | null) => {
    el.current = node;
  };

  useEffect(() => {
    const node = el.current;
    if (!node) return;
    if (active && stuck.current) node.scrollTop = node.scrollHeight;
    // Re-measure once the new content has been laid out.
    const id = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(id);
  }, [dep, active]);

  return { setRef, showButton, onScroll: measure, scrollToBottom };
}
