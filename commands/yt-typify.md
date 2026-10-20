---
description: ref 영상으로 축별 유형화 판정과 아키타입을 만든다
argument-hint: '<category>'
---
카테고리의 ref 영상만으로 축별 유형 판정과 아키타입을 만들어 `refs/ref_<category>/index.md`에 기록한다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
해석 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-typify/SKILL.md` — 결과를 해석하기 전에 읽는다
인자: `$ARGUMENTS` (category)

## 1. 실행

category가 없으면 `ytc refstore categories`로 목록을 보여 주고 고르게 한다.
`ytc cluster run <category>`를 실행한다.

- `status: too_few` → 이렇게 알리고 끝낸다:
  "ref가 {ref}개라 아직 유형을 볼 수 없습니다. {need}개 더 넣으면 예비 관찰을 할 수 있어요. (`/yt-coach:yt-add <category> <링크...>`)
   지금 단계에서는 `/yt-coach:yt-inspire`로 기법 모티브를 먼저 뽑아 볼 수 있습니다."
- `preliminary`(5~9개) → "예비 관찰입니다. 10개부터 정식 판정이 나옵니다"를 먼저 말한다.
- mine 영상은 스크립트가 항상 뺀다. 사용자가 mine을 넣어 달라고 해도 넣지 않고 이유를 말한다: 내 영상을 기준에 섞으면 진단이 자기 자신과 비교하게 된다.

## 2. 해석과 확인

yt-typify 규칙대로 축별 근거 문장과 아키타입 이름을 만들고, 규칙 4절 형식으로 보여 준 뒤 이름을 확인받는다.
- 판정이 이상해 보이면(예: 섹션 라벨이 잘못 붙은 영상 때문) 해당 영상 md를 고치고 다시 돌리면 반영된다고 안내한다.

## 3. 저장

확인을 받으면 notes JSON을 `workspace/tmp/typify_notes.json`에 Write로 쓰고
`ytc cluster apply <category> --notes workspace/tmp/typify_notes.json`
- `invalid_notes` → message대로 고쳐 다시 실행한다.

## 4. 마무리

```
✅ index.md에 판정·아키타입 {n}개·이상치를 기록했습니다 → refs/ref_<category>/index.md
다음: /yt-coach:yt-inspire <category> 로 이 유형을 내 주제에 적용한 모티브를 뽑아 보세요.
```
