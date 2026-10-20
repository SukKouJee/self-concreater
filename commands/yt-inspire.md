---
description: 레퍼런스에서 모티브 카드(흐름·기법·각도·조합)를 뽑는다
argument-hint: '<category> [주제] | <category> star <card-id>'
---
레퍼런스 분석을 바탕으로 "무엇을, 어떤 흐름·촬영으로 만들지" 모티브 카드를 만들어 `refs/ref_<category>/index.md`의 모티브 카드 섹션에 쌓는다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
카드 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-inspire/SKILL.md` — 카드를 만들기 전에 읽는다
인자: `$ARGUMENTS`

## 0. ★ 표시만 할 때

인자가 `<category> star <card-id...>`(또는 "3번 ★" 같은 요청)면 `ytc motif star <category> <card-id>`만 실행하고 끝낸다. 해제는 `--off`.

## 1. 입력 모으기

- 첫 단어 = category(없으면 `ytc refstore categories`로 고르게 한다). 나머지 = 주제(선택).
- `ytc motif context <category> [--topic "<주제>"]`
- ref가 0개면 "먼저 /yt-coach:yt-add 로 레퍼런스를 넣어 주세요"라고 하고 끝낸다.
- `profile`이 null이면 "프로필이 없어 일반적인 카드가 됩니다. /yt-coach:yt-init을 권합니다"라고 한 줄 알리고 계속한다.
- 3단계인데 `typify`가 null이면 "/yt-coach:yt-typify를 먼저 하면 흐름·조합 카드가 나옵니다"라고 알리고, 이번에는 기법·각도 카드만 만든다.

## 2. 기법 카탈로그 정리

규칙 7절에 해당하는 중복이 있으면 병합 계획을 `workspace/tmp/merge_plan.json`에 쓰고
`ytc motif merge-techniques <category> --plan workspace/tmp/merge_plan.json` (병합 결과는 한 줄로 알린다). 없으면 건너뛴다.

## 3. 카드 만들기와 확인

규칙대로 카드를 만들어 6절 형식으로 보여 주고 묻는다: "이 카드들을 저장할까요? (네 / 빼거나 고칠 카드 / 다시)"

## 4. 저장

카드 JSON을 `workspace/tmp/cards.json`에 Write로 쓰고 `ytc motif add <category> --cards workspace/tmp/cards.json`
- `invalid_cards` → message대로 고쳐 다시 실행한다.
- 저장 후 사용자가 고른 번호가 있으면 해당 id마다 `ytc motif star <category> <id>`.

## 5. 마무리

```
✅ 모티브 카드 {n}장 저장 (M-…-007 ~ 012) → refs/ref_<category>/index.md
★ 표시: M-…-008
다음: /yt-coach:yt-draft <category> <카드 id> "<주제>" 로 대본과 샷 리스트를 만들어 보세요.
```
