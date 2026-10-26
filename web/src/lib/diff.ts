// Word-level diff (longest common subsequence) to highlight how a tailored bullet
// rewords its original. Small inputs (one bullet), so O(n·m) is fine.
export type Piece = { text: string; kind: "same" | "added" | "removed" };

export function wordDiff(before: string, after: string): Piece[] {
  const a = before.split(/(\s+)/);
  const b = after.split(/(\s+)/);
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: Piece[] = [];
  let i = 0;
  let j = 0;
  const push = (text: string, kind: Piece["kind"]) => {
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.text += text;
    else out.push({ text, kind });
  };
  while (i < n && j < m) {
    if (a[i] === b[j]) push(a[i++], (j++, "same"));
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) push(a[i++], "removed");
    else push(b[j++], "added");
  }
  while (i < n) push(a[i++], "removed");
  while (j < m) push(b[j++], "added");
  return out;
}
