"""저장소 인터페이스(S3 설계 §3): 모듈은 경로 대신 키(`scripts.keys`)로 이 저장소들을 쓴다.

- 저장소는 바이트 계층이다(ADR-2). YAML 프런트매터·JSON 들여쓰기·줄바꿈은 지금처럼 모듈·`refstore`가 만든다.
- 없는 키는 `FileNotFoundError`(ADR-3). 문서 버전은 `sha256(raw).hexdigest()`(ADR-4).
- 로컬판 구현은 `scripts.filestore`. 서버판은 `Backend`가 저장소를 만들어 `Ctx`에 넣는다(S5).
"""
from __future__ import annotations

import builtins
import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol

from scripts import refstore


@dataclass(frozen=True)
class Meta:
    key: str
    size: int
    mtime: float          # os.stat().st_mtime 그대로 (ns/1e9로 다시 만들지 않는다: 마지막 자리 차이)
    mtime_ns: int         # conti_history 캐시 스탬프용


@dataclass(frozen=True)
class Doc:
    key: str
    raw: bytes            # 디스크 바이트 그대로(BOM 포함)
    label: str            # 오류 메시지용 표시 문자열. FileStore = str(root / key) (지금 메시지와 동일)

    @property
    def version(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @cached_property
    def parsed(self) -> tuple[dict[str, Any], str]:
        """(프런트매터, 본문). 파싱 오류는 지금과 같은 ScriptError(`<label>: ...`)."""
        return refstore.parse_md(self.raw, self.label)

    @property
    def nl(self) -> str:
        """프런트매터 줄바꿈: BOM을 뗀 바이트가 `---\\r\\n`으로 시작하면 CRLF."""
        return "\r\n" if self.raw.removeprefix(b"\xef\xbb\xbf").startswith(b"---\r\n") else "\n"


class VersionConflict(Exception):
    """expect_version과 지금 바이트의 버전이 다르다. 모듈이 자기 ScriptError 코드(conti_changed, ref_changed …)로 바꿔 던진다."""


class DocStore(Protocol):
    """사용자 md 문서. 키는 반드시 `.md`로 끝난다."""

    def read(self, key: str) -> Doc: ...                                   # 없으면 FileNotFoundError
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> Meta: ...
    def write(self, key: str, data: bytes, *, expect_version: str | None = None) -> str: ...  # 원자적, 새 version
    def list(self, prefix: str, pattern: str = "*.md") -> builtins.list[Meta]: ...   # prefix 바로 아래, 순서 §3.1
    def folders(self, prefix: str, pattern: str) -> builtins.list[str]: ...   # "refs/"+"ref_*" → ["refs/ref_a/"]
    def folder_exists(self, prefix: str) -> bool: ...
    def ensure_folder(self, prefix: str) -> None: ...
    def label(self) -> str: ...                                            # 화면·status의 "work_root" 표시
    # §3.2 ctx.local_file이 md 키도 이것으로 푼다(claude CLI Read 등). File = 진짜 경로
    def local_path(self, key: str) -> AbstractContextManager[Path]: ...


class ObjectStore(Protocol):
    """사용자별 바이너리·JSON·HTML. 키는 `.md`로 끝나지 않는다."""

    def get(self, key: str) -> bytes: ...                                  # 없으면 FileNotFoundError
    def put(self, key: str, data: bytes) -> None: ...                      # 원자적, 부모 폴더 생성
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> Meta: ...
    def list(self, prefix: str, pattern: str = "*") -> builtins.list[Meta]: ...   # 파일만, prefix 바로 아래
    def folder_exists(self, prefix: str) -> bool: ...
    def delete(self, key: str) -> None: ...                                # 없어도 된다
    def delete_prefix(self, prefix: str) -> None: ...                      # rmtree(ignore_errors) 대응
    def local_path(self, key: str) -> AbstractContextManager[Path]: ...    # 읽기용 실제 파일
    def write_path(self, key: str) -> AbstractContextManager[Path]: ...    # 외부 도구가 쓸 경로
    def display_path(self, key: str) -> str: ...                           # abs_html·abs_path 출력용


class SharedCache(ObjectStore, Protocol):
    """영상 ID 기준 캐시(사용자 무관). 키는 `workspace/{raw,cuts,frames,shots}/` 아래만(그 밖이면 ValueError)."""


class Scratch(Protocol):
    """로컬 실행 보조. 서버판은 프로세스 임시 폴더, local_path·workdir는 NotImplementedError."""

    def tmp_path(self, name: str) -> Path: ...          # workspace/tmp/<name>, 부모 생성
    def local_path(self, *parts: str) -> Path: ...      # workspace/<parts>
    def arg_path(self, value: str | Path) -> Path: ...  # CLI 인자: 절대면 그대로, 상대면 root 기준
    def key_of(self, value: str | Path) -> str: ...     # 경로 → 키. root 밖이면 ScriptError("bad_path")
    def workdir(self) -> Path: ...                      # claude CLI cwd


class Trash(Protocol):
    # -> {"trash": "workspace/trash/<stamp>", "moved": [...]}. 폴더 키는 끝 `/` 없이(있어도 된다)
    def move(self, keys: builtins.list[str], last: str) -> dict[str, Any]: ...


class Backend(Protocol):
    """서버판(S5)이 `Ctx(backend=...)`로 주입한다. 사용자별 저장소를 만든다."""

    def store(self, user_id: str | None) -> DocStore: ...
    def objects(self, user_id: str | None) -> ObjectStore: ...
    def shared(self, user_id: str | None) -> SharedCache: ...
    def scratch(self, user_id: str | None) -> Scratch: ...
    def trash(self, user_id: str | None) -> Trash: ...
