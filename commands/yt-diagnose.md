---
description: 내 콘텐츠(링크·draft·대본)를 아키타입과 축별로 비교한다
argument-hint: '<category> <link|draft파일|대본파일> | <category> intentional <name> <gap-key> [--off]'
---
내 영상·기획안·대본을 카테고리 아키타입과 축별로 비교해 `mine/<name>.md`에 기록한다. 결과는 아키타입을 바꾸지 않는다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
서술 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-diagnose/SKILL.md` — 결과를 쓰기 전에 읽는다
인자: `$ARGUMENTS`

## 0. 의도한 차이 표시만 할 때

`<category> intentional <name> <gap-key>`(또는 "hook 차이는 일부러 그런 거야")면
`ytc similarity intentional <category> <name> <gap-key>`만 실행하고 "다음 진단부터 이 차이는 빼고 봅니다"라고 알린다. 해제는 `--off`.

## 1. 대상 준비 (세 가지)

- **유튜브 링크** → 내 영상이다. `/yt-coach:yt-add`의 2단계 흐름(fetch → cuts → context → 분석 → 확인 → write)을 role `mine`으로 그대로 따른다
  (`${CLAUDE_PLUGIN_ROOT}/commands/yt-add.md`와 yt-analyze 규칙 참고). 대상 = `refs/ref_<category>/videos/<id>.md`.
  이미 같은 영상이 ref로 있으면(role_conflict) 그대로 알리고 멈춘다.
- **draft 파일**(`drafts/*.md`) → 그대로 대상이다.
- **대본 텍스트**(파일이나 붙여 넣은 글) → 대본을 섹션으로 나눠(yt-analyze 라벨·훅·서사 규칙) draft JSON을 만들되
  `"script_only": true`, 섹션마다 `shots` 없이, 시간은 읽는 속도(한국어 초당 약 5자)로 어림한다.
  `workspace/tmp/draft.json`에 쓰고 `ytc draft save <category> --draft workspace/tmp/draft.json` → 저장된 draft가 대상. (촬영·pacing 축은 빠진다)

## 2. 비교

`ytc similarity compare <category> <대상 경로>`
- `no_typify_result` / `no_archetype` → "비교할 아키타입이 없습니다. ref를 모아 /yt-coach:yt-typify를 먼저 해 주세요"(ref 수는 `ytc refstore count`).
- `preliminary: true` → "예비 진단입니다(ref 10개 미만)"를 먼저 말한다.

## 3. 서술과 확인

yt-diagnose 규칙대로 notes를 만들고 5절 형식으로 보여 준다. 갭마다 "일부러 다르게 한 거면 말해 주세요. 다음 진단·미션에서 뺍니다"라고 한 줄 덧붙인다.
사용자가 의도한 차이라고 하면 `ytc similarity intentional <category> <name> <key>` 후 compare를 다시 해서 갭 목록을 갱신한다.

## 4. 저장

notes JSON을 `workspace/tmp/diag_notes.json`에 Write로 쓰고 `ytc similarity save <category> <대상 경로> --notes workspace/tmp/diag_notes.json`
- `invalid_notes` → message대로 고쳐 다시 실행한다.

```
✅ 진단 기록 → mine/<name>.md (가장 가까운 아키타입 A1 · 갭 2개)
다음: /yt-coach:yt-mission today 에서 이 갭을 연습 미션으로 받을 수 있습니다.
```
