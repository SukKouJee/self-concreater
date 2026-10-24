---
description: 모티브 카드나 아키타입으로 대본 골격과 샷 리스트를 만든다
argument-hint: '<category> <motif-id|archetype> "<주제>"'
---
모티브 카드(또는 아키타입)를 바탕으로 섹션별 대본과 샷 리스트를 만들어 `drafts/`에 저장한다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
기획 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-draft/SKILL.md` — 만들기 전에 읽는다
인자: `$ARGUMENTS`

## 1. 인자

- `<category> <source> "<주제>"`. source는 모티브 카드 id(`M-mukbang-003`), 아키타입 id(`A1`), 또는 지인 기획안 틀 `library:<id>`(규칙 6절. 기획안은 항상 내 카테고리에 저장).
- source가 없으면 index.md의 ★ 카드를 먼저 제안하고(`ytc motif context <category>`의 cards에서 starred), 없으면 아키타입 목록을 보여 주고 고르게 한다.
- 주제가 없으면 카드의 주제 또는 profile의 관심 주제에서 하나 제안한다.

## 2. 만들기

`ytc draft context <category> <source> [--topic "<주제>"]` → 규칙대로 draft JSON을 만들고 5절 형식으로 보여 준 뒤 확인을 받는다.

## 3. 저장

draft JSON을 `workspace/tmp/draft.json`에 Write로 쓰고 `ytc draft save <category> --draft workspace/tmp/draft.json`
- `invalid_draft` → message대로 고쳐 다시 실행한다.
- 결과의 `compare.max_gap`이 0.1보다 크면 어느 섹션이 아키타입과 다른지 한 줄로 알려 준다(의도한 변형이면 괜찮다).

## 4. 마무리

```
✅ 기획안 저장 → drafts/2026-10-20-편의점-컵라면-맵기-4단계-도전.md
찍고 나서 영상 링크로, 또는 지금 이 파일로 /yt-coach:yt-diagnose <category> <파일|링크> 를 해 보세요.
```
