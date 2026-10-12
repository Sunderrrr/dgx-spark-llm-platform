// Line diff for the playground's file edits (red/green view).
//
// Classic LCS on lines, after trimming the common prefix and suffix: an
// edit touches a few lines of a file, so what remains to align is small.
// Past a size bound the table would cost too much memory for the browser:
// we then fall back to « everything removed / everything added », which is
// still correct, just less precise.

export type DiffLine = {
  kind: "same" | "add" | "del";
  text: string;
  /** Line number in the old file (same/del) or the new one (add). */
  oldNo?: number;
  newNo?: number;
};

const MAX_CELLS = 4_000_000;

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }

  const out: DiffLine[] = [];
  for (let i = 0; i < start; i++) out.push({ kind: "same", text: a[i], oldNo: i + 1, newNo: i + 1 });

  const ma = a.slice(start, endA);
  const mb = b.slice(start, endB);
  if (ma.length * mb.length > MAX_CELLS) {
    ma.forEach((text, i) => out.push({ kind: "del", text, oldNo: start + i + 1 }));
    mb.forEach((text, j) => out.push({ kind: "add", text, newNo: start + j + 1 }));
  } else {
    // lcs[i][j] = LCS length of ma[i:] and mb[j:].
    const lcs: Uint32Array[] = Array.from({ length: ma.length + 1 }, () => new Uint32Array(mb.length + 1));
    for (let i = ma.length - 1; i >= 0; i--) {
      for (let j = mb.length - 1; j >= 0; j--) {
        lcs[i][j] = ma[i] === mb[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < ma.length || j < mb.length) {
      if (i < ma.length && j < mb.length && ma[i] === mb[j]) {
        out.push({ kind: "same", text: ma[i], oldNo: start + i + 1, newNo: start + j + 1 });
        i++; j++;
      } else if (j < mb.length && (i >= ma.length || lcs[i][j + 1] >= lcs[i + 1][j])) {
        out.push({ kind: "add", text: mb[j], newNo: start + j + 1 });
        j++;
      } else {
        out.push({ kind: "del", text: ma[i], oldNo: start + i + 1 });
        i++;
      }
    }
  }
  for (let k = endA; k < a.length; k++) {
    out.push({ kind: "same", text: a[k], oldNo: k + 1, newNo: k - endA + endB + 1 });
  }
  return out;
}

/** Changed lines with `context` unchanged lines around them; `null` marks
 *  an elided stretch. */
export function hunks(lines: DiffLine[], context = 3): (DiffLine | null)[] {
  const keep = new Array(lines.length).fill(false);
  lines.forEach((l, i) => {
    if (l.kind === "same") return;
    for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k++) keep[k] = true;
  });
  const out: (DiffLine | null)[] = [];
  let skipped = false;
  lines.forEach((l, i) => {
    if (keep[i]) {
      out.push(l);
      skipped = false;
    } else if (!skipped) {
      out.push(null);
      skipped = true;
    }
  });
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === "add") added++;
    else if (l.kind === "del") removed++;
  }
  return { added, removed };
}
