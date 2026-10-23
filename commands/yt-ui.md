---
description: 작업 폴더를 브라우저 화면(로컬 전용)으로 연다
argument-hint: '[stop]'
---
yt-coach 기능을 내 PC에서만 열리는 브라우저 화면으로 쓰게 한다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
인자: `$ARGUMENTS`

## 1. 인자가 `stop`이면

`ytc ui --stop`을 실행하고 결과를 알린다.
- `{"status": "stopped"}` → "화면 서버를 껐습니다."
- `{"status": "not_running"}` → "켜져 있는 화면 서버가 없습니다."

## 2. 그 외(인자 없음)

`ytc ui --detach`를 실행한다. 서버는 이 세션과 분리된 프로세스로 뜬다.
- `status`가 `started` 또는 `running`이면 `url`을 클릭할 수 있는 링크로 준다. `running`이면 "이미 켜져 있어 그 주소를 그대로 씁니다"를 덧붙인다.
- 아래 안내를 함께 준다.

```
🖥 화면 → <url>
Claude Code를 닫아도 화면은 남습니다. 끄려면 화면의 '서버 끄기' 또는 /yt-coach:yt-ui stop
화면은 이 PC에서만 열리며, 주소의 key가 있어야 접근됩니다(주소를 남에게 보내지 마세요).
```

## 3. 오류

결과가 `{"error": ..., "message": ...}`이면 `message`를 그대로 보여 주고 멈춘다. 추측으로 원인을 덧붙이지 않는다.
