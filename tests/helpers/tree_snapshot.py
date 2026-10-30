"""저장소 이전(S3)용 안전장치: 작업 폴더의 파일 바이트와 함수 반환 JSON을 기록해 이전 전후를 비교한다.

- `copy_workfolder(dst)`: 실제 모양 축소본(`tests/fixtures/workfolder/`)을 임시 폴더로 복사한다.
  픽스처는 `.gitattributes`의 `-text`로 줄바꿈 변환 없이 저장된다(LF·CRLF·BOM 그대로).
  픽스처 안 `workspace/`는 `.gitignore`에 걸리므로 새 파일은 `git add --force`로 넣는다.
- `tree(root)`: `{상대 경로(POSIX): sha256}`. 같은 바이트면 같은 값.
- `capture(root, fn, ...)`: 함수를 부르고 반환값(JSON으로 한 번 왕복한 값 = CLI 출력과 같은 모양)과 그 뒤의 트리를 함께 돌려준다.
- `changes(before, after)`: 더해진·없어진·바뀐 경로.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "workfolder"


def copy_workfolder(dst: Path) -> Path:
    """픽스처 작업 폴더를 dst(없어야 함)로 복사하고 dst를 돌려준다."""
    shutil.copytree(FIXTURE, dst)
    return dst


def tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """root 아래 모든 파일의 `{상대 경로: sha256}`. skip은 건너뛸 상대 경로 접두사."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if skip and rel.startswith(skip):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "modified": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


@dataclass(frozen=True)
class Capture:
    result: Any
    tree: dict[str, str]


def capture(root: Path, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Capture:
    result = fn(*args, **kwargs)
    return Capture(result=json.loads(json.dumps(result, ensure_ascii=False)), tree=tree(root))
