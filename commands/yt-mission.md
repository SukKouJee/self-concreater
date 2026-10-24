---
description: 오늘의 15~30분 미션을 주거나 제출·건너뛰기·교체를 처리한다
argument-hint: '[today | submit <file> | skip [사유] | swap]'
---
하루 15~30분짜리 미션을 주고, 제출물에 짧은 피드백을 남긴다. 기록은 `missions/<날짜>.md`. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
미션 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-mission/SKILL.md` — 미션·피드백을 쓰기 전에 읽는다
인자: `$ARGUMENTS` (없으면 today)

## today

`ytc mission context`
- `today`가 있으면 오늘 미션을 다시 보여 주고 끝낸다(상태가 submitted면 "오늘은 끝났어요", 피드백 한 줄).
- `profile`이 null이면 "/yt-coach:yt-init으로 프로필부터 만들면 미션이 맞춤으로 나옵니다"라고 하고, 관찰 미션 하나로 진행한다.
- 규칙대로 미션 JSON을 만들어 보여 준다:
```
🎯 오늘의 미션 (20분 · 촬영 · 관찰 단계)  ← recommended.adjust가 easier/harder면 "지난 평가 반영해 5분 줄였어요"
탑뷰 손 클로즈업 15초 찍기
왜: ★M-mukbang-001의 핵심 샷 연습
1. …
2. …
결과물: …
```
- "이걸로 할까요? (네 / 바꿔 줘)" → 네면 `workspace/tmp/mission.json`에 쓰고 `ytc mission save --mission workspace/tmp/mission.json`

## submit <file>

- 제출물을 확인하고(이미지·텍스트면 Read, 영상이면 사용자 설명을 듣는다) 규칙 5절대로 피드백을 쓴 뒤 난이도를 묻는다.
- `ytc mission submit "<file>" --difficulty <easy|right|hard> --feedback "<피드백>" [--note "<한 일 한두 줄>"]`
- `no_mission` → "오늘 미션이 없습니다. /yt-coach:yt-mission today"

## skip [사유]

- 사유가 없으면 한 번만 묻는다(시간 부족 / 재료 없음 / 하기 싫음 / 기타). 다그치지 않는다.
- `ytc mission skip --reason "<사유>"` → "다음 미션은 그에 맞춰 조정할게요."

## swap

- 오늘 미션과 다른 재료·종류로 새 미션을 만들어 보여 주고, 확인 후 `ytc mission save --mission workspace/tmp/mission.json --replace`

## 자동 실행 (선택)

매일 아침 미션을 받고 싶다면 README의 cron 예시를 안내한다.
