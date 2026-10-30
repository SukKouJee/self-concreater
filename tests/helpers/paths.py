"""테스트 준비용 실제 경로(로컬 파일 저장소 기준). 운영 코드는 키만 쓰고, 테스트만 파일을 직접 만들거나 본다."""
from __future__ import annotations

from pathlib import Path

from scripts import ctx, footage


def footage_base(fid: str) -> Path:
    """처리 캐시 폴더 `<작업 폴더>/workspace/footage/<fid>`(지금 문맥의 작업 폴더 기준)."""
    root = ctx.here().root
    assert root is not None
    return root / footage.footage_key(fid)
