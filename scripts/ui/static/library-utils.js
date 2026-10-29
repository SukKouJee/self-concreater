// Pure helpers of the 공용 라이브러리 screen. No imports and no DOM access, so node tests load this file directly.

// The repository's name only ("git@github.com:me/friends-lib.git" -> "friends-lib", "(로컬) t7lib.git" -> "(로컬) t7lib"):
// the screen never shows an address or a folder path. Nothing to show -> "연결됨".
export function repoName(remote) {
  const parts = String(remote || "").split(/[\\/:]/).filter(Boolean);
  return (parts.length ? parts[parts.length - 1] : "").replace(/\.git$/i, "") || "연결됨";
}
