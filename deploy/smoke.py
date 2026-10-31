"""컨테이너 스택 스모크: build -> up --wait -> /healthz -> /api/status -> down -v.

사용법:  python deploy/smoke.py local [--project ytc-t3] [--image ytc:local-t3] [--with-youtube]

- 임시 작업 폴더와 가짜 API 키만 쓴다(실제 Claude 호출 없음). 표준 라이브러리만 쓴다.
- `--with-youtube`: tests/fixtures/links.txt의 첫 링크 1개로 `scripts.fetch`와 `scripts.cuts`(360p 다운로드·장면 분석)를
  컨테이너 안에서 돌려 yt-dlp가 JS 런타임 없이 되는지 본다(LLM 호출 없음, 네트워크 사용). 기본은 끔.
- 종료 코드 0이면 통과. 끝나면 항상 `docker compose down -v`로 정리한다(이미지는 남긴다).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = ROOT / "deploy" / "local"
FAKE_KEY = "sk-ant-SMOKE-NOT-A-REAL-KEY"
UP_TIMEOUT_SEC = 180
LINKS = ROOT / "tests" / "fixtures" / "links.txt"


class SmokeFailure(Exception):
    pass


def check(cond: bool, message: str) -> None:
    if not cond:
        raise SmokeFailure(message)
    print(f"ok  {message}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(cmd: list[str], env: dict[str, str], timeout: int = 900, ok_codes: tuple[int, ...] = (0,)) -> str:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if proc.returncode not in ok_codes:
        raise SmokeFailure(f"{' '.join(cmd[:6])} ... 종료 코드 {proc.returncode}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    return proc.stdout + proc.stderr


def http(url: str, key: str | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers={"X-YTC-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, body = r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def parse_ps(text: str) -> list[dict]:
    """`docker compose ps --format json`은 버전에 따라 JSON 배열이거나 줄마다 객체(NDJSON)다."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return [json.loads(ln) for ln in text.splitlines() if ln.strip().startswith("{")]


def first_link() -> str:
    for line in LINKS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    raise SmokeFailure("tests/fixtures/links.txt에 링크가 없습니다")


def smoke_local(project: str, image: str, with_youtube: bool) -> None:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="ytc-smoke-") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / "work"
        work.mkdir()
        env_file = tmp_path / "smoke.env"
        env_file.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        override = tmp_path / "override.yml"
        override.write_text(f'services:\n  ytc:\n    env_file:\n      - "{env_file.as_posix()}"\n', encoding="utf-8")
        env = dict(os.environ, YTC_WORK_DIR=str(work), YTC_PORT=str(port), YTC_IMAGE=image)
        if hasattr(os, "getuid"):                      # Linux: 임시 폴더 소유자와 컨테이너 uid를 맞춘다
            env.update(YTC_UID=str(os.getuid()), YTC_GID=str(os.getgid()))
        compose = ["docker", "compose", "-p", project, "-f", str(LOCAL_DIR / "docker-compose.yml"), "-f", str(override)]
        try:
            run(compose + ["up", "-d", "--build", "--wait", "--wait-timeout", str(UP_TIMEOUT_SEC)], env, timeout=1500)
            check(True, "build + up --wait (healthcheck healthy)")

            logs = run(compose + ["logs", "--no-log-prefix", "ytc"], env)
            m = re.search(r'"url":\s*"(http://127\.0\.0\.1:%d/\?key=([^"&]+))"' % port, logs)
            check(m is not None, "로그에 ?key= 주소가 나온다")
            assert m is not None
            base, key = f"http://127.0.0.1:{port}", m.group(2)

            ps = json.loads("[" + ",".join(run(compose + ["ps", "--format", "json"], env).split("\n")[:-1] or ["{}"]) + "]")
            publishers = [p for row in ps for p in row.get("Publishers") or []]
            check(bool(publishers) and all(p["URL"] == "127.0.0.1" for p in publishers), "게시 포트는 127.0.0.1에만 열린다")

            status, body = http(f"{base}/healthz")
            check(status == 200 and body == {"ok": True}, "/healthz 200 {ok: true} (인증 없음)")
            status, _ = http(f"{base}/api/status")
            check(status == 403, "키 없이 /api/status는 403")
            status, body = http(f"{base}/api/status", key)
            check(status == 200 and isinstance(body, dict), f"/api/status 200 (키 있음) keys={sorted(body)[:6] if isinstance(body, dict) else body}")
            status, body = http(f"{base}/api/features", key)
            disabled = body.get("disabled", {}) if isinstance(body, dict) else {}
            check(status == 200 and {"library", "caption_video"} <= set(disabled), "/api/features: library·caption_video 비활성")

            uid = run(compose + ["exec", "-T", "ytc", "id", "-u"], env).strip()
            check(uid != "0", f"비루트로 실행(uid={uid})")
            engine = run(compose + ["exec", "-T", "ytc", "python", "-c",
                                     "from scripts import llm; print(llm.config())"], env).strip()
            check("anthropic-api" in engine, f"엔진 env 고정 (yt-coach.json 없이): {engine}")
            check((work / "workspace" / "ui.lock").is_file(), "작업 폴더 볼륨에 workspace/ui.lock 생성")

            if with_youtube:
                url = first_link()
                # 자막 없는 영상은 no_transcript(종료 코드 1)지만 yt-dlp 메타데이터 수집은 끝난 상태(raw_status: fetched)다
                out = run(compose + ["exec", "-T", "ytc", "python", "-m", "scripts.fetch", url], env, timeout=300,
                          ok_codes=(0, 1))
                fetched = json.loads(out.strip().splitlines()[-1])
                check(fetched.get("error") in (None, "no_transcript") and bool(fetched.get("title")),
                      f"yt-dlp 메타데이터 수집 통과: {fetched.get('title')} ({fetched.get('error', 'ok')})")
                out = run(compose + ["exec", "-T", "ytc", "python", "-m", "scripts.cuts", url], env, timeout=600)
                check('"error"' not in out, f"360p 다운로드·장면 분석 통과: {out.strip()[:160]}")
        finally:
            try:                                           # 정리 실패가 원래 실패를 가리지 않게 한다
                run(compose + ["down", "-v", "--remove-orphans"], env, timeout=300)
                print("down -v 완료", flush=True)
            except (SmokeFailure, subprocess.TimeoutExpired) as e:
                print(f"WARN down -v 실패(수동 정리 필요: docker compose -p {project} down -v): {e}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="deploy/smoke.py")
    parser.add_argument("edition", choices=["local"])
    parser.add_argument("--project", default=f"ytc-smoke-{os.getpid()}")
    parser.add_argument("--image", default=f"ytc:smoke-{os.getpid()}")
    parser.add_argument("--with-youtube", action="store_true")
    args = parser.parse_args(argv)
    try:
        smoke_local(args.project, args.image, args.with_youtube)
    except SmokeFailure as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
