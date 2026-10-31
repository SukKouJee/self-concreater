"""S3 게이트(설계 §5 "0"의 기계적 정의): 허용 목록 밖 `scripts/**/*.py`는 작업 폴더 경로를 직접 다루지 않는다.

1. `work_root(`·`workspace_dir(`·`category_dir(`·`existing_category_dir(`·`move_to_trash(` 호출 0.
   그 이름의 `from … import`(별칭 포함)·`getattr(x, "<이름>")`도 같은 위반이다
2. 작업 폴더 최상위 이름으로 시작하는 경로형 문자열 상수, `ref_`/`-conti`로 경로를 조립하는 상수 0 (독스트링 제외).
   경로를 만드는 자리는 `/`·`joinpath`·`Path(…)`·`os.path.join(…)`. f-문자열 조각은 `/refs/`처럼 중간에 든 최상위 이름도 센다.
   카테고리 폴더 키에 `index.md`·`techniques.md`·`videos/`를 이어 붙이는 것(`+`, f-문자열)도 센다(ADR-5: `keys.*_md(_at)`)
3. `ctx.root`·`here().root`, `here()`·`current()`·`use()`로 받은 변수의 `.root` 속성 접근 0
4. 작업 폴더를 스스로 정하는 것 0: 문자열 상수 `"YT_COACH_HOME"`, `Path.cwd()`·`os.getcwd()` 호출
키 문자열은 `scripts/keys.py`에서만 만든다. 예외는 아래 `EXEMPT_*`에 이유와 함께 적는다.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import count_work_root

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
SERVER = PLUGIN_ROOT / "server"

FORBIDDEN_CALLS = set(count_work_root.NAMES)
TOP_NAMES = ("refs", "drafts", "mine", "missions", "reviews", "reports", "workspace",
             "profile.md", "yt-coach.json", "library.json")
# 규칙 2의 "경로형" 상수. 맨 단어(`"mine"`=role 값, `"refs"`=사용자 문구 …)는 그것만으로 경로가 아니므로
# 경로를 만드는 자리(`/` 피연산자, `joinpath`·`Path(…)` 인자)에서만 센다.
_TOP_PATH_RE = re.compile(r"^(?:(?:refs|drafts|mine|missions|reviews|reports|workspace)/|"
                          r"(?:profile\.md|yt-coach\.json|library\.json)$)")
_BARE_TOP_RE = re.compile(r"^(?:refs|drafts|mine|missions|reviews|reports|workspace|profile\.md|yt-coach\.json|library\.json)$")
# 카테고리 폴더(`ref_<카테고리>`)·콘티 파일(`<stem>-conti.html`)을 조립하는 조각. `ref_frame` 같은 필드 이름은 아니다
_COMPOSE_RE = re.compile(r"(?<![-A-Za-z0-9])(?:ref_(?:$|\*|\{)|-conti)")
# (CLI 플래그 `--conti`와 `yt-conti` 같은 이름은 앞 글자 검사로 제외한다)
# f-문자열 조각 안의 최상위 이름: `f"{root}/refs/{c}"`의 `/refs/`, `f"{root}/profile.md"`의 `/profile.md`
_FSTR_TOP_RE = re.compile(r"(?:^|[/\\])(?:(?:refs|drafts|mine|missions|reviews|reports|workspace)[/\\]|"
                          r"(?:profile\.md|yt-coach\.json|library\.json)$)")
# 카테고리 안 파일 이름: 폴더 키에 이어 붙이면(`prefix + "index.md"`, `f"{folder}videos/…"`) keys.*_md(_at)를 우회한다
CATEGORY_FILES = ("index.md", "techniques.md", "videos/")
HOME_ENV = "YT_COACH_HOME"
CWD_CALLS = {"cwd", "getcwd"}
_CTX_CALLS = {"here", "current", "use"}

# 규칙 2 예외: (scripts 기준 상대 경로, 상수 값) -> 이유. 새 예외를 늘리지 말고 먼저 keys.* 로 옮긴다.
EXEMPT_CONSTANTS: dict[tuple[str, str], str] = {
    ("scripts/library.py", "refs/remotes/origin/"): "git 원격 추적 ref 이름(작업 폴더 경로가 아니다)",
}


# 서버판(서버 설계 §2 게이트 ②③): `server/**`도 같은 규칙. 경로를 직접 다루는 모듈만 이유와 함께 허용한다.
# 키 문자열은 server/에서도 만들지 않는다(`scripts/keys.py`).
SERVER_ALLOWED: dict[str, str] = {
    "server/scratch.py": "ServerScratch: 작업 중 임시 파일·폴더(로컬 디스크)",
    "server/files.py": "/files 프록시: 요청 경로를 키로 바꾸고 임시 파일을 다룬다",
    "server/admin.py": "admin import: 로컬 작업 폴더를 걸어 키로 올리는 CLI",
}


def _server_modules() -> list[Path]:
    return [p for p in sorted(SERVER.rglob("*.py")) if p.relative_to(PLUGIN_ROOT).as_posix() not in SERVER_ALLOWED]


def _modules() -> list[Path]:
    return [p for p in sorted(SCRIPTS.rglob("*.py")) if p.relative_to(PLUGIN_ROOT).as_posix()
            not in count_work_root.ALLOWED]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _path_operand_nodes(tree: ast.AST) -> set[int]:
    """`x / "refs"`, `"refs" / x`, `.joinpath("refs")`, `Path("refs")`의 상수 피연산자 id."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            ids.update(id(o) for o in (node.left, node.right))
        elif isinstance(node, ast.Call) and (_call_name(node.func) in {"joinpath", "Path", "PurePosixPath"}
                                             or _is_path_join(node.func)):
            ids.update(id(a) for a in node.args)
    return ids


def _is_path_join(func: ast.expr) -> bool:
    """`os.path.join`·`posixpath.join`·`ntpath.join`(문자열의 `", ".join`은 아니다)."""
    return isinstance(func, ast.Attribute) and func.attr == "join" and not isinstance(func.value, ast.Constant) \
        and ast.unparse(func.value) in {"os.path", "posixpath", "ntpath", "path"}


def _concat_nodes(tree: ast.AST) -> set[int]:
    """`x + "index.md"`의 상수 피연산자, `f"{x}videos/…"`처럼 자리 표시 바로 뒤에 오는 f-문자열 조각의 id."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            ids.update(id(o) for o in (node.left, node.right))
        elif isinstance(node, ast.JoinedStr):
            ids.update(id(v) for prev, v in zip(node.values, node.values[1:])
                       if isinstance(prev, ast.FormattedValue) and isinstance(v, ast.Constant))
    return ids


def _fstring_parts(tree: ast.AST) -> set[int]:
    return {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
            for v in node.values if isinstance(v, ast.Constant)}


def _ctx_names(tree: ast.AST) -> set[str]:
    """`x = ctx.here()`, `x = current()`, `with ctx.use(...) as x`처럼 실행 문맥을 받은 변수 이름(모듈 전체 기준)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None \
                and isinstance(node.value, ast.Call) and _call_name(node.value.func) in _CTX_CALLS:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.withitem) and isinstance(node.context_expr, ast.Call) \
                and _call_name(node.context_expr.func) in _CTX_CALLS and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
    return names


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_ctx_expr(node: ast.expr, bound: set[str]) -> bool:
    """`ctx.root`, `c.root`(Ctx 변수), `here().root`, `ctx.here().root`, `ctx.current().root`, 문맥을 받은 변수의 `.root`."""
    if isinstance(node, ast.Call):
        return _call_name(node.func) in _CTX_CALLS
    if isinstance(node, ast.Name):
        return node.id in {"ctx", "c", "cx"} or node.id in bound
    if isinstance(node, ast.Attribute):
        return node.attr == "ctx"
    return False


def violations(path: Path, source: str | None = None) -> list[str]:
    rel = path.relative_to(PLUGIN_ROOT).as_posix() if path.is_absolute() else path.as_posix()
    tree = ast.parse(source if source is not None else path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    joined = _path_operand_nodes(tree)
    fparts = _fstring_parts(tree)
    concat = _concat_nodes(tree)
    bound = _ctx_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in FORBIDDEN_CALLS:
            found.append(f"{rel}:{node.lineno} call {_call_name(node.func)}(")
        elif isinstance(node, ast.Call) and _call_name(node.func) == "getattr" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in FORBIDDEN_CALLS:
            found.append(f"{rel}:{node.lineno} getattr {node.args[1].value}")
        elif isinstance(node, ast.Call) and _call_name(node.func) in CWD_CALLS:
            found.append(f"{rel}:{node.lineno} call {_call_name(node.func)}(")
        elif isinstance(node, ast.ImportFrom):
            found.extend(f"{rel}:{node.lineno} import {a.name}" for a in node.names if a.name in FORBIDDEN_CALLS)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            value = node.value
            pathy = value == HOME_ENV or (" " not in value and (
                _TOP_PATH_RE.match(value) or _COMPOSE_RE.search(value)
                or (_BARE_TOP_RE.match(value) and id(node) in joined)
                or (id(node) in fparts and _FSTR_TOP_RE.search(value))
                or (id(node) in concat and value.startswith(CATEGORY_FILES))))
            if pathy and (rel, value) not in EXEMPT_CONSTANTS:
                found.append(f"{rel}:{node.lineno} string {value[:50]!r}")
        elif isinstance(node, ast.Attribute) and node.attr == "root" and _is_ctx_expr(node.value, bound):
            found.append(f"{rel}:{node.lineno} .root access")
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(SCRIPTS).as_posix())
def test_module_has_no_direct_path_handling(path: Path) -> None:
    assert violations(path) == []


@pytest.mark.parametrize("path", _server_modules(), ids=lambda p: "server/" + p.relative_to(SERVER).as_posix())
def test_server_module_has_no_direct_path_handling(path: Path) -> None:
    assert violations(path) == []


def test_server_allowlist_is_only_existing_or_planned_modules() -> None:
    assert set(SERVER_ALLOWED) <= {"server/scratch.py", "server/files.py", "server/admin.py"}
    for rel in SERVER_ALLOWED:
        assert not (PLUGIN_ROOT / rel).exists() or (PLUGIN_ROOT / rel).is_file()


def test_server_checker_flags_paths_and_keys() -> None:
    src = "k = 'refs/travel'\nr = ctx.root\nc = Path.cwd()\nx = prefix + 'index.md'\n"
    assert len(violations(Path("server/pgstore.py"), src)) == 4
    assert violations(Path("server/pgstore.py"), "k = keys.doc_key(c)\n") == []


def test_count_work_root_outside_is_zero() -> None:
    assert count_work_root.main([])["outside"]["all"] == 0


def test_allowlist_modules_exist() -> None:
    for rel in count_work_root.ALLOWED:
        assert (PLUGIN_ROOT / rel).is_file(), rel


# --- 검사기 자체 -------------------------------------------------------------------------

def _check(src: str) -> list[str]:
    return violations(Path("scripts/x.py"), src)


def test_checker_flags_calls_strings_and_root() -> None:
    assert len(_check("a = common.work_root()\nb = existing_category_dir(c)\nmove_to_trash(p)\n")) == 3
    assert len(_check("p = 'refs/travel'\nq = 'profile.md'\nr = f'ref_{c}'\ns = f'{x}-conti.html'\n")) == 4
    assert len(_check("a = ctx.root\nb = ctx.here().root\nc = here().root\n")) == 3


def test_checker_ignores_docstrings_comments_and_plain_words() -> None:
    src = ('"""refs/travel 와 work_root() 를 설명한다."""\n'
           'def f():\n    """drafts/ 폴더."""\n    # work_root()\n    return "drafts 폴더에 저장했다", "refs", "mine", "ref_frame", "yt-conti", self.root\n')
    assert _check(src) == []


# --- 우회 형태(최종 리뷰 I-1) ---------------------------------------------------------------

def test_checker_flags_import_of_forbidden_name_even_with_alias() -> None:
    assert len(_check("from scripts.common import work_root\n")) == 1
    assert len(_check("from scripts.common import workspace_dir as wd\n")) == 1
    assert len(_check("from scripts.refstore import existing_category_dir as e, read\n")) == 1
    assert _check("from scripts.common import ScriptError\n") == []


def test_checker_flags_getattr_of_forbidden_name() -> None:
    assert len(_check("f = getattr(common, 'work_root')\n")) == 1
    assert len(_check("f = getattr(common, 'move_to_trash', None)\n")) == 1
    assert _check("f = getattr(common, 'emit')\n") == []


def test_checker_flags_os_path_join_operands() -> None:
    assert len(_check("p = os.path.join(base, 'refs', c)\n")) == 1
    assert len(_check("p = posixpath.join(base, 'workspace')\n")) == 1
    assert _check("s = ', '.join(['refs', 'mine'])\n") == []


def test_checker_flags_fstring_path_segments() -> None:
    assert len(_check("p = f'{root}/refs/{c}'\n")) == 1
    assert len(_check("p = f'{root}/workspace/frames/{v}'\n")) == 1
    assert len(_check("p = f'refs/ref_x/{name}'\n")) >= 1
    assert len(_check("p = f'{root}/profile.md'\n")) == 1
    assert _check("m = f'{n}개 레퍼런스를 refs 폴더에서 찾았습니다'\n") == []


def test_checker_flags_home_env_and_cwd() -> None:
    assert len(_check("h = os.environ.get('YT_COACH_HOME')\n")) == 1
    assert len(_check("h = Path.cwd()\n")) == 1
    assert len(_check("h = os.getcwd()\n")) == 1
    assert _check("m = 'YT_COACH_HOME 환경 변수를 확인하세요'\n") == []


def test_checker_flags_root_on_variable_bound_from_here_or_current() -> None:
    assert len(_check("here_ctx = ctx.here()\np = here_ctx.root\n")) == 1
    assert len(_check("cur = ctx.current()\np = cur.root\n")) == 1
    assert len(_check("def f():\n    w = here()\n    return w.root\n")) == 1
    assert len(_check("with ctx.use(ctx.here()) as active:\n    p = active.root\n")) == 1
    assert _check("tree = build()\np = tree.root\n") == []


def test_count_work_root_counts_imports_and_getattr() -> None:
    counts = count_work_root.count_calls(
        "from scripts.common import work_root as w, workspace_dir\nf = getattr(common, 'move_to_trash')\n")
    assert counts["work_root"] == 1 and counts["workspace_dir"] == 1 and counts["move_to_trash"] == 1
    assert counts["all"] == 3


def test_checker_flags_category_file_concatenation() -> None:
    # 카테고리 안 파일 키는 keys.index_md·videos_dir·techniques_md(_at)로 만든다(ADR-5)
    assert len(_check("k = prefix + 'index.md'\n")) == 1
    assert len(_check("k = refstore.require_category(c) + 'videos/'\n")) == 1
    assert len(_check("k = f'{folder}videos/{vid}.md'\n")) == 1
    assert len(_check("k = f'{folder}techniques.md'\n")) == 1
    assert _check("names = ['index.md', 'techniques.md']\nlink = f'[{v}](videos/{v}.md)'\n") == []
