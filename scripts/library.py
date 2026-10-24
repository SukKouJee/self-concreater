"""공용 라이브러리: 지인들과 훅 기법·기획안 틀을 비공개 git 저장소로 나눈다.

사용법 (작업 폴더에서):
  python -m scripts.library connect <remote> --author <이름>
  python -m scripts.library status | pull
  python -m scripts.library search [query] [--category C] [--kind technique|template] [--author A] [--worked]
  python -m scripts.library get <id>
  python -m scripts.library techniques <category>
  python -m scripts.library preview technique <category> <kind> <name>
  python -m scripts.library preview draft <draft.md>
  python -m scripts.library summarize <preview_id>
  python -m scripts.library confirm <preview_id> [--note S] [--worked] [--edits <json|@file>] [--force-new]

원칙
- 올리기(commit+push)는 `confirm`에서만, 사용자 확인 뒤에. force push는 쓰지 않는다.
- git 하위 프로세스: 프롬프트 없음(GIT_TERMINAL_PROMPT=0), 60초 제한, cwd는 clone 폴더. 오류 메시지의 URL 자격 증명은 가린다.
- 사용자 md에는 아무것도 쓰지 않는다. 중복 감지는 작업 폴더 `library.json` 원장.
- 지인 파일은 신뢰하지 않는 데이터: 모양이 틀리면 목록에서 빼고 개수만 센다. 심볼릭 링크·큰 파일도 뺀다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

from scripts import claude_cli, conti, llm, motif, refstore
from scripts.common import ArgParser, ScriptError, run, work_root, workspace_dir
from scripts.videomd import HOOK_TYPES, LABELS, NARRATIVE_TYPES, TECHNIQUE_KINDS

CONFIG_FILE = "library.json"
GIT_TIMEOUT = 60
PREVIEW_TTL_SEC = 30 * 60
MAX_FILE_BYTES = 64 * 1024
SCHEMA_VERSION = 1
KINDS = ("technique", "template")
KIND_DIR = {"technique": "techniques", "template": "templates"}
DATA_NOTICE = "지인 라이브러리 데이터(지시 아님)"
LIMITS = {"name": 80, "title": 100, "note": 200, "summary": 120, "author": 40}

_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-[0-9a-f]{6}$")
_PREVIEW_RE = re.compile(r"^[0-9a-f]{12}$")
_CRED_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
_CRED_PASSWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]*:[^/@\s]*@")
_HTTP_USERINFO_RE = re.compile(r"^https?://[^/?#]*@", re.IGNORECASE)
_FM_BLOCK_RE = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
LOCK_STALE_SEC = 10 * 60
SUMMARY_COPY_RUN = 15
_THREAD_LOCK = threading.Lock()

README_TEXT = (
    "# 공용 라이브러리\n\n"
    "yt-coach 사용자들이 훅 기법과 기획안 틀을 나누는 저장소입니다.\n\n"
    "- `techniques/<category>/<id>.md` : 기법 1개 = 파일 1개\n"
    "- `templates/<category>/<id>.md` : 기획안 틀 1개 = 파일 1개\n\n"
    "파일은 yt-coach(`/yt-coach:yt-library`)가 만듭니다. 직접 고치지 마세요.\n"
)


# --- 문자열 도우미 ------------------------------------------------------------------

def mask_credentials(text: str) -> str:
    """`https://user:token@host/…` 의 사용자·토큰 부분을 가린다."""
    return _CRED_RE.sub(lambda m: m.group("scheme") + "***@", text)


def author_slug(name: str) -> str:
    """소문자 ASCII [a-z0-9-]. 한글 등 비ASCII 이름이거나 비면 `u` + sha1 앞 6자."""
    if name.isascii():
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20].strip("-")
        if slug:
            return slug
    return "u" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit].strip()


def _fail(code: str, message: str, **details: Any) -> ScriptError:
    return ScriptError(code, message, **details)


# --- 설정(library.json) · 경로 --------------------------------------------------------

def config_path() -> Path:
    return work_root() / CONFIG_FILE


def clone_dir() -> Path:
    return workspace_dir("library")


def _read_config() -> dict[str, Any] | None:
    path = config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _fail("library_config", f"{CONFIG_FILE}을 읽지 못했습니다(올바른 JSON이어야 합니다): {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("remote"), str) or not isinstance(data.get("author"), str):
        raise _fail("library_config", f'{CONFIG_FILE}에는 "remote"와 "author" 문자열이 있어야 합니다')
    if not isinstance(data.get("shared"), list):
        data["shared"] = []
    return data


def _write_config(cfg: dict[str, Any]) -> None:
    refstore.write_atomic(config_path(), (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _require_connected() -> tuple[dict[str, Any], Path]:
    cfg = _read_config()
    root = clone_dir()
    if cfg is None or not (root / ".git").exists():
        raise _fail("not_connected", "공용 라이브러리가 연결되지 않았습니다. 먼저 저장소 주소로 연결하세요 (ytc library connect <주소> --author <이름>)")
    return cfg, root


# --- git ----------------------------------------------------------------------------

_AUTH_HINTS = ("authentication failed", "could not read username", "could not read password", "permission denied",
               "terminal prompts disabled", "access denied", "403", "401", "invalid credentials", "publickey",
               "repository not found", "host key verification failed")
_REMOTE_HINTS = ("does not appear to be a git repository", "does not exist", "not a git repository",
                 "no such file or directory", "invalid path", "unable to find remote helper")
_NETWORK_HINTS = ("could not resolve host", "unable to access", "connection refused", "timed out", "network is unreachable",
                  "failed to connect", "could not connect", "connection reset")


def _classify(stderr: str) -> tuple[str, str]:
    low = stderr.lower()
    if any(h in low for h in _AUTH_HINTS):
        return "library_auth", ("저장소에 접근할 권한이 없거나 인증에 실패했습니다. git 자격 증명(자격 증명 관리자·SSH·`gh auth login`)과 "
                                "저장소 초대 여부를 확인하세요. yt-coach는 비밀번호를 묻지 않습니다.")
    if any(h in low for h in _REMOTE_HINTS):
        return "library_remote", "저장소를 찾을 수 없습니다. 주소(경로)가 맞는지 확인하세요."
    if any(h in low for h in _NETWORK_HINTS):
        return "library_network", "저장소에 연결하지 못했습니다. 인터넷 연결과 저장소 주소를 확인하세요."
    return "git_failed", "git 명령이 실패했습니다."


def _tail(text: str, limit: int = 300) -> str:
    return mask_credentials(" ".join(text.split()))[-limit:]


_SSH_CONFIGURED: bool | None = None


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """우리가 띄운 git과 그 자식만 PID로 끈다(이미지 이름으로 끄지 않는다)."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _execute(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """출력은 파이프가 아니라 임시 파일로 받는다: 자식(git-remote-https, ssh 등)이 파이프를 쥐고 있어도 멈추지 않는다."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        extra: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **extra)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise
        out.seek(0)
        err.seek(0)
        return code, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def _git_env(cwd: Path) -> dict[str, str]:
    global _SSH_CONFIGURED
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C", "LANGUAGE": "C"}
    if "GIT_SSH_COMMAND" not in os.environ and "GIT_SSH" not in os.environ:
        if _SSH_CONFIGURED is None:
            code, out, _ = _execute(["git", "config", "--get", "core.sshCommand"], cwd, env, 15)
            _SSH_CONFIGURED = code == 0 and bool(out.strip())
        if not _SSH_CONFIGURED:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"      # 사용자 ssh 설정이 없을 때만: 암호 프롬프트로 멈추지 않게
    return env


def _git(args: list[str], cwd: Path, *, check: bool = True, what: str = "git") -> subprocess.CompletedProcess[str]:
    try:
        env = _git_env(cwd)
        code, out, err = _execute(["git", "-c", "protocol.ext.allow=never", *args], cwd, env, GIT_TIMEOUT)
    except FileNotFoundError as e:
        raise _fail("git_missing", "git을 찾을 수 없습니다. git을 설치하고 PATH에 추가하세요.") from e
    except subprocess.TimeoutExpired as e:
        raise _fail("git_timeout", f"{what}이(가) {GIT_TIMEOUT}초 안에 끝나지 않았습니다. 네트워크와 인증 상태를 확인하세요.") from e
    except OSError as e:
        raise _fail("git_failed", f"git을 실행하지 못했습니다: {mask_credentials(str(e))}") from e
    proc = subprocess.CompletedProcess(args, code, out, err)
    if check and code != 0:
        kind, message = _classify(err + "\n" + out)
        raise _fail(kind, f"{what} 실패: {message} (git: {_tail(err or out)})")
    return proc


def _recover(root: Path) -> None:
    """이전 실패로 남은 rebase가 있으면 중단한다(자동 해결하지 않는다)."""
    git_dir = root / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        _git(["rebase", "--abort"], root, check=False)


@contextmanager
def _exclusive() -> Iterator[None]:
    """confirm·pull을 한 번에 하나씩(프로세스 안 스레드 잠금 + 프로세스 사이 잠금 파일)."""
    busy = _fail("library_busy", "다른 라이브러리 작업이 진행 중입니다. 잠시 뒤 다시 시도하세요.")
    if not _THREAD_LOCK.acquire(blocking=False):
        raise busy
    path = workspace_dir("library.lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    stale = time.time() - path.stat().st_mtime > LOCK_STALE_SEC
                except FileNotFoundError:
                    stale = True
                if attempt == 1 or not stale:
                    raise busy from None
                path.unlink(missing_ok=True)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
    finally:
        _THREAD_LOCK.release()


def _branch(root: Path) -> str:
    out = _git(["symbolic-ref", "--short", "HEAD"], root, what="브랜치 확인").stdout.strip()
    if not out:
        raise _fail("git_failed", "현재 브랜치를 알 수 없습니다(detached HEAD). workspace/library를 확인하세요.")
    return out


def _head(root: Path) -> str | None:
    proc = _git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _remote_has_branch(root: Path, branch: str) -> bool:
    out = _git(["ls-remote", "--heads", "origin", branch], root, what="원격 확인").stdout
    return bool(out.strip())


def _is_tracked(root: Path, rel: str) -> bool:
    return _git(["ls-files", "--error-unmatch", "--", rel], root, check=False).returncode == 0


def _norm_remote(remote: str) -> str:
    return remote.strip().rstrip("/").removesuffix(".git").rstrip("/")


# --- connect · status · pull ----------------------------------------------------------

def _check_remote(remote: str) -> None:
    """주소 모양 검사. http(s)의 userinfo(사용자·토큰)는 모두 거절하고, scp 형식 `git@host:`·`ssh://git@`는 허용한다."""
    if not remote or remote.startswith("-") or "\n" in remote or "\r" in remote:
        raise _fail("invalid_remote", "저장소 주소가 올바르지 않습니다 (예: git@github.com:team/yt-library.git)")
    if _HTTP_USERINFO_RE.match(remote) or _CRED_PASSWORD_RE.match(remote):
        raise _fail("credentials_in_url", "주소에 사용자 이름·비밀번호·토큰을 넣지 마세요. yt-coach는 자격 증명을 저장하지 않습니다. "
                    "git 자격 증명 관리자·SSH·`gh auth login`을 쓰세요.")


def connect(remote: str, author: str) -> dict[str, Any]:
    remote = (remote or "").strip()
    name = _one_line(author or "", LIMITS["author"])
    _check_remote(remote)
    if not name:
        raise _fail("invalid_author", "표시 이름(author)이 필요합니다")
    root = clone_dir()
    existing = _read_config()
    if (root / ".git").exists():
        _recover(root)
        current = _git(["remote", "get-url", "origin"], root, what="원격 주소 확인").stdout.strip()
        if _norm_remote(current) != _norm_remote(remote):
            raise _fail("remote_mismatch", f"이미 다른 저장소({mask_credentials(current)})가 연결돼 있습니다. 바꾸려면 "
                        f"{root} 폴더와 {CONFIG_FILE}을 지우고 다시 연결하세요.")
        status = "already_connected"
    else:
        if root.exists() and any(root.iterdir()):
            raise _fail("clone_dir_conflict", f"{root} 폴더가 비어 있지 않아 clone할 수 없습니다")
        root.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--", remote, str(root)], root.parent, what="저장소 받기(clone)")
        status = "connected"
    cfg = {"remote": remote, "author": name, "shared": (existing or {}).get("shared", []) if existing else [],
           "last_pull": (existing or {}).get("last_pull")}
    _write_config(cfg)
    readme = root / "README.md"
    if not readme.exists():
        refstore.write_atomic(readme, README_TEXT.encode("utf-8"))   # 커밋하지 않는다: 첫 공유 때 함께 올라간다
    return {"status": status, "remote": mask_credentials(remote), "author": name, "slug": author_slug(name),
            "empty": _head(root) is None}


def status() -> dict[str, Any]:
    cfg = _read_config()
    root = clone_dir()
    if cfg is None or not (root / ".git").exists():
        return {"connected": False}
    return {"connected": True, "remote": mask_credentials(cfg["remote"]), "author": cfg["author"],
            "slug": author_slug(cfg["author"]), "last_pull": cfg.get("last_pull"),
            "shared_count": len(cfg["shared"]), "empty": _head(root) is None}


def _with_readme_cleared(root: Path, action: Any) -> Any:
    """추적되지 않는 로컬 README는 원격 README와 덮어쓰기 충돌이 나므로 지웠다가, 끝나면 없을 때만 다시 만든다."""
    readme = root / "README.md"
    if readme.exists() and not _is_tracked(root, "README.md"):
        readme.unlink()
    try:
        return action()
    finally:
        if not readme.exists():
            refstore.write_atomic(readme, README_TEXT.encode("utf-8"))


def pull() -> dict[str, Any]:
    cfg, root = _require_connected()
    with _exclusive():
        _recover(root)
        branch = _branch(root)
        warning = None
        dirty = [line for line in _git(["status", "--porcelain"], root, what="상태 확인").stdout.splitlines()
                 if line.strip() and not line.endswith("README.md")]
        if dirty:
            warning = "라이브러리 폴더에 로컬 변경이 있습니다(yt-coach가 만든 것이 아닙니다). 받기에 영향을 줄 수 있습니다."
        if not _remote_has_branch(root, branch):
            result = "empty"
        else:
            before = _head(root)
            _with_readme_cleared(root, lambda: _git(["pull", "--ff-only", "origin", branch], root, what="받기(pull)"))
            result = "updated" if _head(root) != before else "up_to_date"
        cfg["last_pull"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_config(cfg)
    out: dict[str, Any] = {"status": result, "last_pull": cfg["last_pull"]}
    if warning:
        out["warning"] = warning
    return out


# --- 지인 파일 읽기(신뢰하지 않는 데이터) --------------------------------------------------

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _text(v: Any, limit: int) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= limit


def _validate_common(fm: dict, kind: str, category: str, stem: str) -> None:
    if fm.get("kind") != kind:
        raise ValueError("kind")
    if fm.get("id") != stem or not _ID_RE.match(stem):
        raise ValueError("id")
    if fm.get("category") != category or not _CATEGORY_RE.match(category):
        raise ValueError("category")
    if not _text(fm.get("author"), LIMITS["author"]):
        raise ValueError("author")
    if fm.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version")
    if fm.get("worked") is not None and not isinstance(fm["worked"], bool):
        raise ValueError("worked")
    if fm.get("note") is not None and not _text(fm["note"], LIMITS["note"] * 2):
        raise ValueError("note")
    shared_at = fm.get("shared_at")
    if isinstance(shared_at, (date, datetime)):
        fm["shared_at"] = shared_at.isoformat()[:10]
    elif not (isinstance(shared_at, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", shared_at)):
        raise ValueError("shared_at")


def _validate_technique(fm: dict) -> None:
    if fm.get("technique_kind") not in TECHNIQUE_KINDS:
        raise ValueError("technique_kind")
    if not _text(fm.get("name"), LIMITS["name"] * 2):
        raise ValueError("name")


def _validate_template(fm: dict) -> None:
    if not _text(fm.get("title"), LIMITS["title"] * 2):
        raise ValueError("title")
    if not _is_num(fm.get("target_sec")) or fm["target_sec"] <= 0:
        raise ValueError("target_sec")
    if fm.get("hook_type") not in HOOK_TYPES or fm.get("narrative_type") not in NARRATIVE_TYPES:
        raise ValueError("hook/narrative")
    sections = fm.get("sections")
    if not isinstance(sections, list) or not sections or len(sections) > 30:
        raise ValueError("sections")
    for s in sections:
        if not isinstance(s, dict) or s.get("label") not in LABELS or not _is_num(s.get("ratio")) or not 0 <= s["ratio"] <= 1:
            raise ValueError("section")
        if s.get("sub") is not None and not _text(s["sub"], 30):
            raise ValueError("section sub")
    pacing = fm.get("pacing")
    if (not isinstance(pacing, dict) or set(pacing) - {"avg_cut_sec"}
            or (pacing.get("avg_cut_sec") is not None and not _is_num(pacing["avg_cut_sec"]))):
        raise ValueError("pacing")
    mix = fm.get("shot_mix")
    if mix is not None and (not isinstance(mix, dict) or len(mix) > 20
                            or any(not _text(k, 30) or not _is_num(v) for k, v in mix.items())):
        raise ValueError("shot_mix")


def _reject_yaml_aliases(text: str) -> None:
    """앵커(&)·별칭(*)이 있는 프런트매터는 받지 않는다(별칭 폭탄 방지). 토큰만 훑으므로 펼치지 않는다."""
    m = _FM_BLOCK_RE.match(text)
    if not m:
        raise ValueError("no frontmatter")
    try:
        for token in yaml.scan(m.group(1), Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise ValueError("yaml anchor/alias")
    except yaml.YAMLError as e:
        raise ValueError("yaml") from e


def _parse_item(text: str, kind: str, category: str, stem: str) -> tuple[dict, str]:
    """프런트매터·본문 검증. 모양이 틀리면 ValueError."""
    _reject_yaml_aliases(text)
    try:
        fm, body = refstore.split_frontmatter(text.lstrip("\ufeff"))
    except ScriptError as e:
        raise ValueError(e.code) from e
    _validate_common(fm, kind, category, stem)
    (_validate_technique if kind == "technique" else _validate_template)(fm)
    return fm, body.replace("\r\n", "\n")


def _read_item(path: Path, kind: str, category: str) -> tuple[dict, str]:
    """검증된 (프런트매터, 본문). 모양이 틀리면 ValueError/OSError."""
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("not a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("too large")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError("encoding") from e
    return _parse_item(text, kind, category, path.stem)


def _scan(root: Path) -> tuple[list[tuple[dict, str, Path]], int]:
    items: list[tuple[dict, str, Path]] = []
    skipped = 0
    for kind in KINDS:
        base = root / KIND_DIR[kind]
        if not base.is_dir() or base.is_symlink():
            continue
        for cat_dir in sorted(p for p in base.iterdir()):
            if cat_dir.is_symlink() or not cat_dir.is_dir():
                skipped += 1 if (cat_dir.is_symlink() or cat_dir.suffix == ".md") else 0
                continue
            for path in sorted(cat_dir.glob("*.md")):
                try:
                    fm, body = _read_item(path, kind, cat_dir.name)
                except (ValueError, OSError):
                    skipped += 1
                    continue
                items.append((fm, body, path))
    return items, skipped


def _first_paragraph(body: str, limit: int = 200) -> str:
    para = next((p.strip() for p in body.split("\n\n") if p.strip() and not p.lstrip().startswith("#")), "")
    return " ".join(para.split())[:limit]


def _summary(fm: dict, body: str, mine_ids: set[str]) -> dict[str, Any]:
    kind = fm["kind"]
    out: dict[str, Any] = {
        "id": fm["id"], "kind": kind, "author": fm["author"], "category": fm["category"],
        "worked": bool(fm.get("worked")), "note": fm.get("note"), "shared_at": fm["shared_at"],
        "mine": fm["id"] in mine_ids,       # 내 원장에 있는 id만(이름이 같은 지인을 내 것으로 보지 않는다)
        "data_notice": DATA_NOTICE,
    }
    if kind == "technique":
        out.update(name=fm["name"], technique_kind=fm["technique_kind"], summary=_first_paragraph(body))
    else:
        out.update(title=fm["title"], target_sec=fm["target_sec"], hook_type=fm["hook_type"],
                   narrative_type=fm["narrative_type"])
    return out


def search(query: str | None = None, category: str | None = None, kind: str | None = None,
           author: str | None = None, worked: bool | None = None) -> dict[str, Any]:
    cfg, root = _require_connected()
    if kind is not None and kind not in KINDS:
        raise _fail("invalid_kind", f"kind는 {', '.join(KINDS)} 중 하나여야 합니다: {kind!r}")
    mine_ids = {s.get("id") for s in cfg["shared"] if isinstance(s, dict)}
    needle = (query or "").strip().lower()
    found: list[dict[str, Any]] = []
    items, skipped = _scan(root)
    for fm, body, _ in items:
        if category and fm["category"] != category:
            continue
        if kind and fm["kind"] != kind:
            continue
        if author and fm["author"] != author:
            continue
        if worked is not None and bool(fm.get("worked")) != worked:
            continue
        if needle:
            hay = " ".join(str(x) for x in (fm.get("name"), fm.get("title"), fm.get("note"), fm["author"], body)).lower()
            if needle not in hay:
                continue
        found.append(_summary(fm, body, mine_ids))
    found.sort(key=lambda it: (it["shared_at"], it["id"]), reverse=True)
    return {"items": found, "skipped": skipped}


def _split_parts(body: str) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    heading: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if heading is not None or "".join(buf).strip():
                parts.append({"heading": heading, "text": "\n".join(buf).strip()})
            heading, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if heading is not None or "".join(buf).strip():
        parts.append({"heading": heading, "text": "\n".join(buf).strip()})
    return parts


_FM_KEYS = ("kind", "id", "author", "category", "technique_kind", "name", "title", "target_sec", "hook_type",
            "narrative_type", "pacing", "shot_mix", "worked", "note", "shared_at", "schema_version")


def _plain_fm(fm: dict) -> dict[str, Any]:
    """검증을 통과한 알려진 키만 JSON으로 내보낸다(지인 파일의 엉뚱한 키·YAML 타입이 새지 않게)."""
    out = {k: fm[k] for k in _FM_KEYS if k in fm}
    if "pacing" in fm:
        out["pacing"] = {"avg_cut_sec": fm["pacing"].get("avg_cut_sec")}
    if "sections" in fm:
        out["sections"] = [{k: s[k] for k in ("label", "sub", "ratio") if k in s} for s in fm["sections"]]
    return out


def get(item_id: str) -> dict[str, Any]:
    cfg, root = _require_connected()
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        raise _fail("invalid_id", f"라이브러리 id 형식이 아닙니다: {item_id!r}")
    mine_ids = {s.get("id") for s in cfg["shared"] if isinstance(s, dict)}
    unreadable = False
    for kind in KINDS:
        base = root / KIND_DIR[kind]
        if not base.is_dir() or base.is_symlink():
            continue
        for cat_dir in sorted(base.iterdir()):
            path = cat_dir / f"{item_id}.md"
            if cat_dir.is_symlink() and (path.exists() or path.is_symlink()):
                unreadable = True            # 심볼릭 링크 폴더는 따라가지 않는다
                continue
            if cat_dir.is_symlink() or not cat_dir.is_dir() or not (path.exists() or path.is_symlink()):
                continue
            try:
                fm, body = _read_item(path, kind, cat_dir.name)
            except (ValueError, OSError):
                unreadable = True
                continue
            return {**_summary(fm, body, mine_ids), "frontmatter": _plain_fm(fm), "body": body,
                    "parts": _split_parts(body)}
    if unreadable:
        raise _fail("invalid_item", f"읽을 수 없는 파일입니다(형식이 올바르지 않음): {item_id}")
    raise _fail("item_not_found", f"라이브러리에 없는 항목입니다: {item_id} (먼저 받기)")


# --- 내 기법 · 미리보기 --------------------------------------------------------------------

def _ledger_ids(key: Any) -> list[dict[str, Any]]:
    cfg = _read_config()
    if not cfg:
        return []
    return [s for s in cfg["shared"] if isinstance(s, dict) and s.get("key") == key]


def my_techniques(category: str) -> list[dict[str, Any]]:
    cat = refstore.existing_category_dir(category)
    path = cat / "techniques.md"
    if not path.is_file():
        return []
    _, body = refstore.read_md(path)
    items = motif.parse_techniques(body)
    counts: dict[tuple[str, str], int] = {}
    for it in items:
        counts[(it["kind"], it["name"])] = counts.get((it["kind"], it["name"]), 0) + 1
    out = []
    for it in items:
        key = [category, it["kind"], it["name"]]
        shared = _ledger_ids(key)
        out.append({"key": key, "category": category, "kind": it["kind"], "name": it["name"], "desc": it["desc"],
                    "source_count": len(it["sources"]), "ambiguous": counts[(it["kind"], it["name"])] > 1,
                    "shared_id": shared[-1]["id"] if shared else None})
    return out


def preview_path(preview_id: str) -> Path:
    if not isinstance(preview_id, str) or not _PREVIEW_RE.match(preview_id):
        raise _fail("invalid_preview", f"미리보기 id 형식이 아닙니다: {preview_id!r}")
    return workspace_dir("tmp", f"library_preview_{preview_id}.json")


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _save_preview(data: dict[str, Any]) -> None:
    path = preview_path(data["preview_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    refstore.write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def load_preview(preview_id: str) -> dict[str, Any]:
    path = preview_path(preview_id)
    if not path.is_file():
        raise _fail("preview_not_found", "미리보기가 없습니다(이미 올렸거나 취소됨). 다시 미리보기를 만드세요.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        created = float(data["created_at"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        path.unlink(missing_ok=True)
        raise _fail("preview_not_found", "미리보기 파일이 손상돼 지웠습니다. 다시 미리보기를 만드세요.") from e
    if time.time() - created > PREVIEW_TTL_SEC:
        path.unlink(missing_ok=True)
        raise _fail("preview_expired", "미리보기가 만료됐습니다(30분). 다시 미리보기를 만드세요.")
    return data  # type: ignore[no-any-return]


def discard_preview(preview_id: str) -> dict[str, Any]:
    preview_path(preview_id).unlink(missing_ok=True)
    return {"status": "discarded"}


def _shell(kind: str, category: str, key: Any, fields: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    pid = uuid.uuid4().hex[:12]
    return {"preview_id": pid, "kind": kind, "category": category, "key": key, "created_at": time.time(),
            "fm": fields, "body": body, "note": None, "worked": None}


def _present(data: dict[str, Any]) -> dict[str, Any]:
    left = max(0, int(PREVIEW_TTL_SEC - (time.time() - data["created_at"])))
    return {"preview_id": data["preview_id"], "kind": data["kind"], "category": data["category"],
            "expires_in_sec": left, "frontmatter": data["fm"], "note": data["note"], "worked": data["worked"],
            "markdown": _render_body(data), "duplicate": (_ledger_ids(data["key"]) or [None])[-1]}


def preview_technique(category: str, kind: str, name: str, desc: str | None = None) -> dict[str, Any]:
    if kind not in TECHNIQUE_KINDS:
        raise _fail("invalid_kind", f"기법 종류는 {', '.join(TECHNIQUE_KINDS)} 중 하나여야 합니다: {kind!r}")
    matches = [t for t in my_techniques(category) if t["kind"] == kind and t["name"] == name]
    if desc is not None:
        matches = [t for t in matches if t["desc"] == desc]
    if not matches:
        raise _fail("technique_not_found", f"내 기법 카탈로그에 없습니다: {category}/{kind}/{name}")
    if len(matches) > 1:
        raise _fail("ambiguous_technique", "같은 종류·이름의 기법이 둘 이상입니다. 설명으로 구분하세요: "
                    + " / ".join(m["desc"][:60] for m in matches))
    t = matches[0]
    fields = {"kind": "technique", "author": None, "category": category, "technique_kind": kind,
              "name": _one_line(name, LIMITS["name"])}
    body = {"description": refstore.escape_headings(t["desc"]).strip(), "source_count": t["source_count"]}
    data = _shell("technique", category, t["key"], fields, body)
    data["hash"] = _content_hash([fields, body])
    _save_preview(data)
    return _present(data)


def _round(x: float, n: int = 3) -> float:
    return round(float(x), n)


def _rhythm(shots: list[dict[str, Any]]) -> str:
    groups: dict[str, list[float]] = {}
    for s in shots:
        groups.setdefault(s["size"], []).append(float(s["sec"]))
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return ", ".join(f"{size} {round(sum(v) / len(v), 1):g}초 × {len(v)}" for size, v in ordered[:4])


def preview_template(draft_path: str | Path) -> dict[str, Any]:
    root = work_root()
    path = Path(draft_path)
    path = path if path.is_absolute() else root / path
    drafts = (root / "drafts").resolve()
    try:
        rel = path.resolve().relative_to(drafts)
    except ValueError as e:
        raise _fail("invalid_draft_path", "작업 폴더의 drafts/ 안에 있는 기획안만 틀로 올릴 수 있습니다") from e
    parsed = conti.parse_draft(path)
    fm = parsed["fm"]
    if fm.get("hook_type") not in HOOK_TYPES or fm.get("narrative_type") not in NARRATIVE_TYPES:
        raise _fail("invalid_draft", "기획안 프런트매터의 hook_type·narrative_type이 올바르지 않습니다")
    category = fm.get("category")
    if not isinstance(category, str) or not _CATEGORY_RE.match(category):
        raise _fail("invalid_draft", "기획안 프런트매터의 category가 올바르지 않습니다")
    sections = parsed["sections"]
    total = max(float(s["end"]) for s in sections)
    if total <= 0:
        raise _fail("invalid_draft", "기획안 섹션 길이가 올바르지 않습니다")
    shots_all = [shot for s in sections for shot in s["shots"]]
    weight: dict[str, float] = {}
    for shot in shots_all:
        weight[shot["size"]] = weight.get(shot["size"], 0.0) + float(shot["sec"])
    sum_w = sum(weight.values())
    shot_mix = ({k: _round(v / sum_w, 2) for k, v in sorted(weight.items(), key=lambda kv: -kv[1])}
                if sum_w > 0 else None)
    pacing = fm.get("pacing") if isinstance(fm.get("pacing"), dict) else {}
    avg = pacing.get("avg_cut_sec")
    fields = {
        "kind": "template", "author": None, "category": category,
        "title": _one_line(parsed["title"], LIMITS["title"]),
        "target_sec": _round(fm.get("target_sec") or total, 2),
        "hook_type": fm["hook_type"], "narrative_type": fm["narrative_type"],
        "sections": [{"label": s["label"], **({"sub": s["sub"]} if s.get("sub") else {}),
                      "ratio": _round((float(s["end"]) - float(s["start"])) / total)} for s in sections],
        "pacing": {"avg_cut_sec": _round(avg, 2) if _is_num(avg) else None},
        "shot_mix": shot_mix,
    }
    body = {"summaries": ["" for _ in sections],
            "rhythms": [_rhythm(s["shots"]) if s["shots"] else "" for s in sections]}
    key = "drafts/" + rel.as_posix()
    data = _shell("template", category, key, fields, body)
    data["source"] = key
    data["hash"] = _content_hash([fields, body["rhythms"]])
    _save_preview(data)
    return _present(data)


# --- 섹션 요약(Claude 1회) ------------------------------------------------------------------

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {
        "type": "object",
        "properties": {"label": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["label", "summary"]}}},
    "required": ["sections"],
}


def _shares_run(summary: str, script: str, n: int = SUMMARY_COPY_RUN) -> bool:
    a, b = "".join(summary.split()), "".join(script.split())
    if len(a) < n or len(b) < n:
        return False
    grams = {b[i:i + n] for i in range(len(b) - n + 1)}
    return any(a[i:i + n] in grams for i in range(len(a) - n + 1))


def _validate_summaries(data: Any, labels: list[str], scripts: list[str]) -> list[str]:
    def bad(msg: str) -> ScriptError:
        return ScriptError("invalid_summary", f"섹션 요약이 형식에 맞지 않습니다: {msg}")
    secs = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(secs, list) or len(secs) != len(labels):
        raise bad(f"sections는 {len(labels)}개여야 합니다")
    out: list[str] = []
    for want, got, script in zip(labels, secs, scripts):
        if not isinstance(got, dict) or got.get("label") != want:
            raise bad(f"순서와 label이 기획안과 같아야 합니다(기대: {want})")
        summary = _one_line(got.get("summary") or "", 10_000)
        if not summary:
            raise bad(f"{want}의 summary가 비었습니다")
        if len(summary) > LIMITS["summary"]:
            raise bad(f"{want}의 summary는 한 줄({LIMITS['summary']}자 이내)이어야 합니다")
        if _shares_run(summary, script):
            raise bad(f"{want}의 summary가 대본 원문을 {SUMMARY_COPY_RUN}자 이상 그대로 옮겼습니다. 구간의 역할만 쓰세요")
        out.append(refstore.escape_headings(summary))
    return out


def summarize_template(preview_id: str) -> dict[str, Any]:
    data = load_preview(preview_id)
    if data["kind"] != "template":
        raise _fail("invalid_preview", "기획안 틀 미리보기만 요약할 수 있습니다")
    parsed = conti.parse_draft(work_root() / data["source"])
    secs = parsed["sections"]
    labels = [s["label"] for s in secs]
    if len(secs) != len(data["fm"]["sections"]):
        raise _fail("draft_changed", "미리보기를 만든 뒤 기획안 섹션이 바뀌었습니다. 미리보기를 다시 만드세요.")
    payload = {"title": data["fm"]["title"], "sections": [
        {"label": s["label"], "sub": s.get("sub"), "start": s["start"], "end": s["end"], "script": s["script"],
         "shots": [{"size": x["size"], "sec": x["sec"], "action": x["action"]} for x in s["shots"]]} for s in secs]}
    task = ("이 기획안의 섹션마다 '무엇을 하는 구간인지' 한 줄 요약을 만드세요. 대본 원문을 옮기지 말고 "
            "구간의 역할만 120자 이내로 쓰세요. 섹션 순서와 label은 입력 그대로.")
    _, summaries = llm.ask_validated(
        [claude_cli.SKILLS_DIR / "yt-library" / "SKILL.md"], payload, task,
        lambda d: _validate_summaries(d, labels, [str(x["script"]) for x in secs]), schema=SUMMARY_SCHEMA)
    data["body"]["summaries"] = summaries
    _save_preview(data)
    return _present(data)


# --- 올리기(confirm) -------------------------------------------------------------------------

def _apply_edits(data: dict[str, Any], note: str | None, worked: bool | None, edits: dict[str, Any] | None) -> None:
    edits = edits or {}
    if not isinstance(edits, dict):
        raise _fail("invalid_edits", "edits는 객체여야 합니다")
    unknown = set(edits) - {"title", "note", "worked", "sections"}
    if unknown:
        raise _fail("invalid_edits", f"edits에 쓸 수 없는 키: {', '.join(sorted(unknown))}")
    if "note" in edits:
        data["note"] = edits["note"]
    if "worked" in edits:
        data["worked"] = edits["worked"]
    if note is not None:
        data["note"] = note
    if worked is not None:
        data["worked"] = worked
    if data["note"] is not None:
        line = _one_line(data["note"], LIMITS["note"])
        data["note"] = line or None
    if data["worked"] is not None and not isinstance(data["worked"], bool):
        raise _fail("invalid_edits", "worked는 true/false여야 합니다")
    if "title" in edits:
        if data["kind"] != "template":
            raise _fail("invalid_edits", "title은 기획안 틀에서만 고칠 수 있습니다")
        title = _one_line(edits["title"], LIMITS["title"])
        if not title:
            raise _fail("invalid_edits", "title이 비어 있습니다")
        data["fm"]["title"] = title
    if "sections" in edits:
        if data["kind"] != "template":
            raise _fail("invalid_edits", "sections는 기획안 틀에서만 고칠 수 있습니다")
        labels = [s["label"] for s in data["fm"]["sections"]]
        given = edits["sections"]
        if not isinstance(given, list) or len(given) != len(labels) or any(
                not isinstance(g, dict) or g.get("label") != lab for g, lab in zip(given, labels)):
            raise _fail("invalid_edits", "sections는 미리보기와 같은 순서·label의 [{label, summary}] 여야 합니다")
        data["body"]["summaries"] = [
            refstore.escape_headings(_one_line(g.get("summary") or "", LIMITS["summary"])) for g in given]


def _render_body(data: dict[str, Any]) -> str:
    body = data["body"]
    if data["kind"] == "technique":
        text = body["description"]
        if body.get("source_count"):
            text += f"\n\n출처: 레퍼런스 {body['source_count']}개"
        return text + "\n"
    lines: list[str] = []
    rows = []
    for sec, summary in zip(data["fm"]["sections"], body["summaries"]):
        if summary:
            name = f"{sec['label']}/{sec['sub']}" if sec.get("sub") else sec["label"]
            rows.append(f"- **{name}** ({round(sec['ratio'] * 100)}%): {summary}")
    if rows:                                   # 요약이 하나도 없으면 제목도 만들지 않는다
        lines += ["## 섹션 요약", "", *rows]
    rhythm = [(s, r) for s, r in zip(data["fm"]["sections"], body["rhythms"]) if r]
    if rhythm:
        lines += ["", "## 샷 리듬", ""] if lines else ["## 샷 리듬", ""]
        lines += [f"- {s['label']}: {r}" for s, r in rhythm]
    return "\n".join(lines) + "\n"


def _render_file(data: dict[str, Any], item_id: str, author: str, shared_at: str) -> bytes:
    fm = dict(data["fm"])
    fm["author"] = author
    ordered: dict[str, Any] = {"kind": fm.pop("kind"), "id": item_id}
    ordered.update(fm)
    if data["worked"] is not None:
        ordered["worked"] = data["worked"]
    if data["note"]:
        ordered["note"] = data["note"]
    ordered["shared_at"] = shared_at
    ordered["schema_version"] = SCHEMA_VERSION
    return refstore.join_frontmatter(ordered, _render_body(data)).encode("utf-8")


def _name_of(data: dict[str, Any]) -> str:
    return str(data["fm"].get("name") or data["fm"].get("title"))


def _push_rejected(stderr: str) -> bool:
    low = stderr.lower()
    return any(h in low for h in ("rejected", "non-fast-forward", "fetch first", "failed to push some refs"))


def _rollback(root: Path, before: str | None, rels: list[str], branch: str) -> None:
    """올리기에 실패하면 이번에 만든 커밋·파일을 되돌려 저장소를 깨끗하게 둔다(원격은 건드리지 않음).

    원격 브랜치를 이미 받아 왔으면(rebase가 성공한 뒤 재push가 실패한 경우 등) 그 상태로 맞춘다.
    """
    _recover(root)
    remote_ref = f"refs/remotes/origin/{branch}"
    if _git(["rev-parse", "--verify", "-q", remote_ref], root, check=False).returncode == 0:
        _git(["reset", "--hard", remote_ref], root, check=False)
    elif before is not None:
        _git(["reset", "--hard", before], root, check=False)
    else:
        _git(["update-ref", "-d", "HEAD"], root, check=False)
        _git(["rm", "--cached", "-q", "--ignore-unmatch", "--", *rels], root, check=False)
    for rel in rels:
        if rel != "README.md":
            (root / rel).unlink(missing_ok=True)


def _rebase_pull(root: Path, branch: str) -> None:
    try:
        proc = _git(["pull", "--rebase", "origin", branch], root, check=False)
    except ScriptError:
        _recover(root)                    # 시간 초과 등으로 rebase가 반쯤 남았을 수 있다
        raise
    if proc.returncode == 0:
        return
    text = proc.stderr + "\n" + proc.stdout
    leftover = (root / ".git" / "rebase-merge").exists() or (root / ".git" / "rebase-apply").exists()
    _recover(root)
    if leftover or "conflict" in text.lower():
        raise _fail("rebase_conflict", "다른 사람의 변경과 겹쳐 합치지 못했습니다. 자동으로 해결하지 않고 중단했습니다. "
                    "받기(pull) 후 다시 시도하거나 저장소 관리자에게 알리세요.")
    code, message = _classify(text)
    raise _fail(code, f"최신 내용 합치기(pull --rebase) 실패: {message} (git: {_tail(text)})")


def _sync_before_share(root: Path, branch: str) -> None:
    """올리기 전에 최신화한다. 원격이 비었거나 아직 없는 브랜치면 할 일이 없다."""
    if not _remote_has_branch(root, branch):
        return
    if _head(root) is None:
        _with_readme_cleared(root, lambda: _git(["pull", "--ff-only", "origin", branch], root, what="받기(pull)"))
    else:
        _with_readme_cleared(root, lambda: _rebase_pull(root, branch))


def _remote_has_file(root: Path, branch: str, rel: str) -> bool:
    """push가 시간 초과된 뒤, 원격이 실제로 커밋을 받았는지 확인한다."""
    try:
        _git(["fetch", "origin", branch], root)
    except ScriptError:
        return False          # 확인할 수 없으면 호출한 쪽이 원래의 시간 초과 오류를 그대로 낸다
    return _git(["cat-file", "-e", f"refs/remotes/origin/{branch}:{rel}"], root, check=False).returncode == 0


def _push(root: Path, branch: str, rel: str) -> subprocess.CompletedProcess[str]:
    args = ["push", "origin", f"HEAD:refs/heads/{branch}"]
    try:
        return _git(args, root, check=False)
    except ScriptError as e:
        if e.code == "git_timeout" and _remote_has_file(root, branch, rel):
            return subprocess.CompletedProcess(args, 0, "", "")     # 시간은 초과했지만 원격에 올라가 있다
        raise


def _new_id(root: Path, data: dict[str, Any], author: str, today: str) -> str:
    slug = author_slug(author)
    while True:
        seed = f"{data['hash']}|{time.time_ns()}|{uuid.uuid4().hex}"
        item_id = f"{slug}-{today.replace('-', '')}-{hashlib.sha1(seed.encode()).hexdigest()[:6]}"
        if not any((root / d / data["category"] / f"{item_id}.md").exists() for d in KIND_DIR.values()):
            return item_id


def confirm(preview_id: str, note: str | None = None, worked: bool | None = None,
            edits: dict[str, Any] | None = None, force_new: bool = False) -> dict[str, Any]:
    data = load_preview(preview_id)
    cfg, root = _require_connected()
    prior = _ledger_ids(data["key"])
    if prior and not force_new:
        last = prior[-1]
        raise _fail("duplicate", f"이미 올렸습니다 ({last.get('shared_at')}, {last.get('id')}). 새 파일로 또 올리려면 "
                    "force_new로 다시 확인하세요.", shared_id=last.get("id"), shared_at=last.get("shared_at"))
    _apply_edits(data, note, worked, edits)
    if not _CATEGORY_RE.match(data["category"]):
        raise _fail("invalid_category", f"카테고리 이름이 올바르지 않습니다: {data['category']!r}")
    with _exclusive():
        _recover(root)
        return _share(preview_id, data, cfg, root)


def _share(preview_id: str, data: dict[str, Any], cfg: dict[str, Any], root: Path) -> dict[str, Any]:
    author = cfg["author"]
    branch = _branch(root)
    _sync_before_share(root, branch)

    today = date.today().isoformat()
    item_id = _new_id(root, data, author, today)
    rel = f"{KIND_DIR[data['kind']]}/{data['category']}/{item_id}.md"
    content = _render_file(data, item_id, author, today)
    try:                                       # 지인이 읽지 못할 파일은 올리지 않는다(지인 파일과 같은 검증)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("too large")
        _parse_item(content.decode("utf-8"), data["kind"], data["category"], item_id)
    except ValueError as e:
        raise _fail("invalid_share", f"이 내용은 공용 라이브러리 형식에 맞지 않아 올리지 않았습니다({e}). "
                    "제목·섹션·샷 크기 이름의 길이와 모양을 확인하세요.") from e
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise _fail("invalid_path", "라이브러리 폴더 밖에는 쓸 수 없습니다")
    target.parent.mkdir(parents=True, exist_ok=True)
    refstore.write_atomic(target, content)
    rels = [rel]
    readme = root / "README.md"
    if not readme.exists():
        refstore.write_atomic(readme, README_TEXT.encode("utf-8"))
    if not _is_tracked(root, "README.md"):
        rels.append("README.md")

    before = _head(root)
    try:
        _git(["add", "--", *rels], root, what="파일 추가(add)")
        message = _one_line(f"share({data['category']}): {_name_of(data)} by {author}", 200)
        slug = author_slug(author)
        _git(["-c", f"user.name={author}", "-c", f"user.email={slug}@yt-coach.local", "-c", "commit.gpgsign=false",
              "commit", "-q", "-m", message, "--", *rels], root, what="커밋(commit)")
        push = _push(root, branch, rel)
        if push.returncode != 0 and _push_rejected(push.stderr):
            _rebase_pull(root, branch)
            push = _push(root, branch, rel)
            if push.returncode != 0 and _push_rejected(push.stderr):
                raise _fail("library_busy", "다른 사람이 동시에 올렸습니다. 잠시 뒤 다시 올리세요")
        if push.returncode != 0:
            code, why = _classify(push.stderr + "\n" + push.stdout)
            raise _fail(code, f"올리기(push) 실패: {why} (git: {_tail(push.stderr or push.stdout)})")
    except ScriptError:
        _rollback(root, before, rels, branch)
        raise
    commit = _head(root)
    cfg["shared"].append({"key": data["key"], "hash": data["hash"], "id": item_id, "shared_at": today})
    _write_config(cfg)
    preview_path(preview_id).unlink(missing_ok=True)
    unsummarized = sum(1 for x in data["body"].get("summaries", []) if not x)
    out: dict[str, Any] = {"status": "shared", "id": item_id, "path": rel, "commit": commit, "pushed": True}
    if unsummarized:
        out["unsummarized"] = unsummarized
    return out


# --- CLI ---------------------------------------------------------------------------------------

def _load_edits(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        if value.startswith("@"):
            edits_path = Path(value[1:])
            text = (edits_path if edits_path.is_absolute() else work_root() / edits_path).read_text(encoding="utf-8")
        else:
            text = value
        data = json.loads(text)
    except (OSError, ValueError) as e:
        raise _fail("invalid_edits", f"--edits를 읽지 못했습니다(JSON 문자열 또는 @파일): {e}") from e
    if not isinstance(data, dict):
        raise _fail("invalid_edits", "--edits는 JSON 객체여야 합니다")
    return data


def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.library")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("remote")
    c.add_argument("--author", required=True)
    sub.add_parser("status")
    sub.add_parser("pull")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="?")
    s.add_argument("--category")
    s.add_argument("--kind")
    s.add_argument("--author")
    s.add_argument("--worked", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("id")
    t = sub.add_parser("techniques")
    t.add_argument("category")
    p = sub.add_parser("preview")
    psub = p.add_subparsers(dest="what", required=True)
    pt = psub.add_parser("technique")
    pt.add_argument("category")
    pt.add_argument("kind")
    pt.add_argument("name")
    pd = psub.add_parser("draft")
    pd.add_argument("file")
    sm = sub.add_parser("summarize")
    sm.add_argument("preview_id")
    cf = sub.add_parser("confirm")
    cf.add_argument("preview_id")
    cf.add_argument("--note")
    cf.add_argument("--worked", action="store_true")
    cf.add_argument("--edits")
    cf.add_argument("--force-new", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "connect":
        return connect(args.remote, args.author)
    if args.cmd == "status":
        return status()
    if args.cmd == "pull":
        return pull()
    if args.cmd == "search":
        return search(args.query, args.category, args.kind, args.author, True if args.worked else None)
    if args.cmd == "get":
        return get(args.id)
    if args.cmd == "techniques":
        return {"techniques": my_techniques(args.category)}
    if args.cmd == "preview":
        if args.what == "technique":
            return preview_technique(args.category, args.kind, args.name)
        return preview_template(args.file)
    if args.cmd == "summarize":
        return summarize_template(args.preview_id)
    return confirm(args.preview_id, args.note, True if args.worked else None, _load_edits(args.edits), args.force_new)


if __name__ == "__main__":
    run(main)
