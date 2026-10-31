# syntax=docker/dockerfile:1
# 빌드 컨텍스트는 저장소 루트다(deploy/Dockerfile.dockerignore가 허용 목록으로 컨텍스트를 줄인다).
#   docker build -f deploy/Dockerfile --target local -t ytc:local .
# Remotion(Node·Chrome)은 넣지 않는다: 자막 영상 만들기는 컨테이너에서 쓸 수 없다(YTC_DISABLE=caption_video).

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS base

# OpenCV는 headless 휠, PyAV 휠에 ffmpeg가 들어 있어 시스템 ffmpeg는 없다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저(소스가 바뀌어도 이 레이어는 캐시). 테스트 도구(pytest)는 런타임에 필요 없어 뺀다.
# opencv 교체: scenedetect가 GUI판 opencv-python을 의존성으로 끌어와, headless와 같은 cv2 폴더를 덮어쓴다.
# GUI판은 libxcb 등 X11 라이브러리가 있어야 import 되므로(slim 이미지에는 없음) 지우고 headless를 다시 깐다.
# (--no-deps 재설치는 numpy 등 이미 깔린 의존성을 건드리지 않기 위함. 아래 import 검사가 결과를 확인한다.)
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    grep -v '^pytest' requirements.txt > /tmp/requirements.runtime.txt \
    && pip install -r /tmp/requirements.runtime.txt \
    && pip uninstall -y opencv-python \
    && pip install --force-reinstall --no-deps opencv-python-headless \
    && rm /tmp/requirements.runtime.txt \
    && python -c "import cv2, av, scenedetect, yt_dlp"

COPY scripts ./scripts
COPY templates ./templates
COPY skills ./skills
COPY docs/help ./docs/help

# 비루트 고정 uid. 작업 폴더(/work)는 이 uid 소유로 만들어 두고, compose가 호스트 uid로 바꿔 실행할 수도 있다.
# HOME·캐시는 임의 uid에서도 쓸 수 있게 /tmp 아래에 둔다.
RUN groupadd --gid 10001 ytc \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ytc \
    && mkdir /work && chown 10001:10001 /work

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    YTC_IN_CONTAINER=1

USER 10001
ENTRYPOINT ["tini", "--"]


FROM base AS local

# 게시 포트와 내부 포트는 같은 번호(Host 헤더 검사). 기본 8765, compose가 --port로 덮는다.
ENV YT_COACH_HOME=/work \
    YTC_LLM_ENGINE=anthropic-api \
    YTC_DISABLE=library,caption_video
WORKDIR /app
VOLUME ["/work"]
EXPOSE 8765
# 맨 docker run 도 점검되도록(compose의 healthcheck와 같은 /healthz). 포트는 컨테이너의 YTC_PORT(없으면 8765).
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('YTC_PORT', '8765'), timeout=3)"]
CMD ["python", "-m", "scripts.ui", "--host", "0.0.0.0", "--port", "8765", "--no-open", "--foreground"]


FROM base AS server

# 서버판(서버 설계 §9.1): base + requirements-server.txt + server/. 테스트 도구는 넣지 않는다.
# requirements-server.txt 첫 줄 `-r requirements.txt`는 뺀다: base가 이미 깔았고, 다시 풀면 scenedetect가 GUI판 opencv를 되돌려 끌어온다.
USER root
COPY requirements-server.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    grep -v '^-r ' requirements-server.txt > /tmp/requirements.server.txt \
    && pip install -r /tmp/requirements.server.txt \
    && rm /tmp/requirements.server.txt \
    && python -c "import cv2, av, scenedetect, yt_dlp, starlette, uvicorn, psycopg, psycopg_pool, boto3, alembic, authlib"
COPY server ./server
# 상태 폴더(timings.json·.last_fetch)와 임시 폴더: 이름 있는 볼륨은 처음 붙일 때 이 소유권을 이어받는다.
RUN mkdir -p /var/lib/ytc/state /tmp/ytc \
    && chown 10001:10001 /var/lib/ytc/state /tmp/ytc

ENV YTC_LLM_ENGINE=anthropic-api \
    YTC_STATE_DIR=/var/lib/ytc/state \
    YTC_SCRATCH_DIR=/tmp/ytc
USER 10001
VOLUME ["/var/lib/ytc/state"]
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
# 접근 로그는 끈다(쿼리 문자열의 OIDC code·state가 로그에 남지 않게, 서버 설계 §4.2). --lifespan on: 설정·저장소 오류면 시작 단계에서 끝난다.
CMD ["uvicorn", "server.asgi:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--lifespan", "on", "--no-access-log"]
