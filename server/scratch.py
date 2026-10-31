"""서버판 Scratch(서버 설계 §3.2·§3.4): `scripts.stores.Scratch`를 프로세스 임시 폴더로 구현한다.

- 임시 핸드오프 파일(`workspace/tmp/<name>` = `keys.tmp(name)`)은 사용자마다 `<scratch_root>/<uid>/<name>`.
  같은 프로세스 안에서 HTTP 요청 → 작업 스레드로 넘길 때만 쓴다(ADR-S6). 시작할 때 `clear_scratch_root`로 비운다.
- `local_path`: `"tmp"` = 그 사용자의 임시 폴더, `timings.json`·`.last_fetch` = 서버 전역 상태 폴더
  (볼륨, 모든 사용자가 같은 파일: `.last_fetch`가 서버 전체 YouTube 속도 제한이다).
  그 밖(공용 라이브러리 clone·lock 등)은 NotImplementedError.
- `arg_path`: 절대 경로는 자기 임시 폴더 안만, 상대 경로는 임시 폴더 기준. 밖이면 ScriptError("bad_path").
- `key_of`: 상대 값은 키(`..`·`./`는 FileScratch의 resolve처럼 접어서 본 뒤 `keys.check`), 자기 임시 폴더 안 절대 경로는
  `workspace/tmp/…`, 그 밖은 bad_path. 서버에는 작업 폴더가 없으므로 다른 절대 경로는 키가 될 수 없다.
- `workdir()`(claude CLI cwd)는 서버판에 없다: 서버는 `anthropic-api` 엔진만 쓴다(ADR-S10).
"""
from __future__ import annotations

import posixpath
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from scripts import keys as _keys
from scripts.common import ScriptError

# local_path names that are machine state shared by the whole server (state volume)
STATE_FILES: Final = frozenset({"timings.json", ".last_fetch"})
TMP_PART: Final = "tmp"


def clear_scratch_root(root: Path) -> None:
    """서버 시작 때 임시 폴더를 비운다(전 프로세스가 남긴 사용자 폴더). 이름이 UUID인 하위 폴더만 지운다:
    설정 실수로 다른 폴더를 가리켜도 사용자 데이터가 아닌 것은 건드리지 않는다."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        try:
            uuid.UUID(child.name)
        except ValueError:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)


class ServerScratch:
    """사용자 한 명의 임시 폴더(`<root>/<uid>/`)와 서버 전역 상태 폴더."""

    def __init__(self, root: Path, state_dir: Path, user_id: uuid.UUID | str) -> None:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
        self._tmp = Path(root) / str(uid)
        self._state = Path(state_dir)

    # --- 임시 폴더 ---

    def _tmp_root(self) -> Path:
        self._tmp.mkdir(parents=True, exist_ok=True)
        return self._tmp

    def tmp_path(self, name: str) -> Path:
        key = _keys.tmp(name)                                  # bad_path for empty names, '..', '\\', ':'
        path = self._tmp_root().joinpath(*key.removeprefix(_keys.TMP).split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def tmp_file(self, key: str) -> Iterator[Path]:
        """임시 키(`keys.tmp(name)`)의 실제 경로(`tmp_path(name)`). 다른 키는 ValueError."""
        if _keys.kind(_keys.check(key)) != "scratch":
            raise ValueError(f"임시 파일 키가 아닙니다: {key!r}")
        yield self.tmp_path(key.removeprefix(_keys.TMP))

    def local_path(self, *parts: str) -> Path:
        if parts == (TMP_PART,):
            return self._tmp_root()
        if len(parts) == 1 and parts[0] in STATE_FILES:
            self._state.mkdir(parents=True, exist_ok=True)
            return self._state / parts[0]
        raise NotImplementedError(
            f"서버판에는 이 로컬 파일이 없습니다({'/'.join(parts)}): "
            "공용 라이브러리 같은 로컬 전용 기능은 내 PC 판에서만 됩니다")

    # --- 인자·키 ---

    def _inside_tmp(self, path: Path, value: str | Path) -> Path:
        real = path.resolve()
        if not real.is_relative_to(self._tmp.resolve()):
            raise ScriptError("bad_path", f"작업 폴더 밖의 경로입니다: {value}")
        return real

    def arg_path(self, value: str | Path) -> Path:
        """CLI·핸드오프 인자 경로. 절대면 자기 임시 폴더 안만(그대로 돌려준다), 상대면 임시 폴더 기준."""
        path = Path(value)
        if path.is_absolute():
            self._inside_tmp(path, value)
            return path
        joined = self._tmp_root() / path
        self._inside_tmp(joined, value)
        return joined

    def key_of(self, value: str | Path) -> str:
        path = Path(value)
        if path.is_absolute():
            real = self._inside_tmp(path, value)
            rel = real.relative_to(self._tmp.resolve()).as_posix()
            if rel == ".":
                raise ScriptError("bad_path", f"작업 폴더 자체는 파일이 아닙니다: {value}")
            return _keys.tmp(rel)
        text = value.as_posix() if isinstance(value, Path) else value
        if not text or "\\" in text:
            return _keys.check(text)                           # bad_path with the usual message
        rel = posixpath.normpath(text)
        if rel == ".":
            raise ScriptError("bad_path", f"작업 폴더 자체는 파일이 아닙니다: {value}")
        if rel == ".." or rel.startswith("../") or rel.startswith("/"):
            raise ScriptError("bad_path", f"작업 폴더 밖의 경로입니다: {value}")
        return _keys.check(rel)

    def workdir(self) -> Path:
        raise NotImplementedError("claude CLI 엔진(작업 폴더 cwd)은 내 PC 판에서만 씁니다. "
                                  "서버판은 anthropic-api 엔진을 씁니다")
