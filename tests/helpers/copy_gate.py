"""서버판 문구 게이트 도우미: 내 PC(로컬판)에서만 뜻이 통하는 말과, 소스에서 사용자에게 보일 수 있는 문자열 뽑기."""
from __future__ import annotations

import ast
from pathlib import Path

# 서버판(웹) 화면·응답·도움말에 나오면 안 되는 말. 작업 폴더·터미널·명령어·Claude Code 채팅 안내 같은 내 PC 전용 개념.
FORBIDDEN: tuple[str, ...] = (
    "작업 폴더", "작업폴더", "터미널", "ytc ", "localhost", "127.0.0.1", "Claude Code", "슬래시",
    "Claude에게", "플러그인", "pip install", "/yt-",
    "서버 끄기", "UI 서버", "명령줄", "환경변수", "YT_PROXY", "yt-coach.json",
)

# A finding: (file, line, term, text). An allowlist entry (file, term, prefix) covers findings in that file with that
# term whose string starts with prefix, so a new sentence with the same word in the same file is not let through.
Finding = tuple[str, int, str, str]


def allowed(finding: Finding, allowlist: dict[tuple[str, str, str], str]) -> bool:
    rel, _, term, text = finding
    return any(rel == f and term == t and text.startswith(prefix) for f, t, prefix in allowlist)


def stale(findings: list[Finding], allowlist: dict[tuple[str, str, str], str]) -> list[tuple[str, str, str]]:
    """Allowlist entries that no longer match anything (the allowlist stays tight)."""
    return [key for key in allowlist
            if not any(r == key[0] and t == key[1] and text.startswith(key[2]) for r, _, t, text in findings)]


def hits(text: str) -> list[str]:
    return [term for term in FORBIDDEN if term in text]


def js_strings(source: str) -> list[tuple[int, str]]:
    """JS 소스의 문자열 리터럴('…', "…", `…`)을 (줄 번호, 내용)으로. 주석은 건너뛴다. 템플릿의 ${…}는 내용째 둔다.
    정규식 리터럴은 따로 다루지 않는다(이 앱의 정적 JS에서 따옴표가 든 정규식이 문구로 잘못 잡히면 허용 목록이 아니라 여기를 고친다)."""
    return [(line, text) for _, line, text in _js_literals(source)]


def copy_server_strings(source: str) -> list[tuple[int, str]]:
    """copy.js에서 서버판에 보일 수 있는 문자열: `local:` 바로 뒤의 리터럴(로컬판 문구)만 뺀 나머지 전부."""
    return [(line, text) for start, line, text in _js_literals(source)
            if not source[:start].rstrip().endswith("local:")]


def _js_literals(source: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    i, n, line = 0, len(source), 1
    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
        elif source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += source.count("\n", i, j)
            i = j
        elif c in "'\"`":
            start_line, j, buf = line, i + 1, []
            while j < n and source[j] != c:
                if source[j] == "\\" and j + 1 < n:
                    buf.append(source[j:j + 2])
                    j += 2
                    continue
                if source[j] == "\n":
                    line += 1
                buf.append(source[j])
                j += 1
            out.append((i, start_line, "".join(buf)))
            i = j + 1
        else:
            i += 1
    return out


def _is_ed_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ed" and len(node.args) == 2


def py_strings(source: str) -> list[tuple[int, str]]:
    """파이썬 소스의 문자열 상수(f-string의 상수 조각 포함)를 (줄 번호, 내용)으로. docstring과 `ed(local, server)`의
    첫 인자(로컬판 문구)는 뺀다: 서버판에서 보일 수 있는 문자열만 남는다."""
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if _is_ed_call(node):
            for sub in ast.walk(node.args[0]):
                skip.add(id(sub))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            out.append((node.lineno, node.value))
    return out


def scan(paths: list[Path], extract, root: Path) -> list[Finding]:
    """(파일, 줄, 금지어, 문자열 앞부분) 목록."""
    found = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        for lineno, text in extract(path.read_text(encoding="utf-8")):
            for term in hits(text):
                found.append((rel, lineno, term, text[:80]))
    return found
