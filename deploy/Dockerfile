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
# scenedetect가 GUI판 opencv-python을 끌어와 같은 cv2 폴더를 덮어쓰므로(libxcb 필요) 지우고 headless를 다시 깐다.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    grep -v '^pytest' requirements.txt > /tmp/requirements.runtime.txt \
    && pip install -r /tmp/requirements.runtime.txt     && pip uninstall -y opencv-python     && pip install --force-reinstall --no-deps opencv-python-headless \
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
CMD ["python", "-m", "scripts.ui", "--host", "0.0.0.0", "--port", "8765", "--no-open", "--foreground"]
