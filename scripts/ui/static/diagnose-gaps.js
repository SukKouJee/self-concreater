// Pure gap-set rules for the diagnose screen (no DOM, so they are tested under node).
//   savedGaps: gap keys in the saved record; intentional: keys now marked intentional;
//   currentKeys: keys of the current compare (intentional gaps are not part of it).

const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

// The saved explanation no longer fits: a gap appeared or came back that the record does not cover.
// Gaps the user marked intentional since the save are expected to be missing, so they do not count.
export function recordMismatch(savedGaps, intentional, currentKeys) {
  const expected = savedGaps.filter((k) => !intentional.includes(k));
  return !sameSet(expected, currentKeys);
}

// An open preview blocks saving only when the current compare has a gap its notes do not cover.
// Vanished gaps are fine: the server drops their notes.
export function previewStale(previewKeys, currentKeys) {
  return currentKeys.some((k) => !previewKeys.includes(k));
}
