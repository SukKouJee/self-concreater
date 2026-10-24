---
description: 저장된 기획안의 훅·제목·썸네일을 다듬는다 (훅 5개·제목 10개·썸네일 지시서, 점수·근거 포함)
argument-hint: '<draft 파일 | latest>'
---
저장된 기획안의 훅·제목·썸네일 후보를 만들고, 한국어 기준 점수·지적과 내 레퍼런스 근거를 붙여 보여 준 뒤, 사용자가 고른 것만 기획안에 반영한다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
다듬기 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-polish/SKILL.md` — 만들기 전에 읽는다
인자: `$ARGUMENTS`

## 1. 인자

- draft 파일 경로(`drafts/…md`, 콘티 `-conti.md`는 아님).
- 비어 있거나 `latest`면 `drafts/`에서 `-conti`가 아닌 md를 최근 순으로 최대 5개 보여 주고(제목·날짜) 고르게 한다. `latest`면 가장 최근 것을 고르고 한 줄로 알린다. drafts가 비어 있으면 `/yt-coach:yt-draft`를 먼저 안내하고 멈춘다.

## 2. 만들기

`ytc polish context <draft>` →
- `hook`이 null이면 "훅 섹션이 없어 훅 대본은 바뀌지 않습니다. 제목·썸네일만 다듬습니다"라고 알린다(훅 후보는 만들되 참고용).
- `refs.ref`가 0이면 "레퍼런스가 없어 근거 문장은 비어 있습니다"라고 알린다.
- 규칙대로 polish JSON을 만들어 `workspace/tmp/polish.json`에 Write로 쓰고 `ytc polish evaluate <draft> --data workspace/tmp/polish.json`을 실행한다.
  - `invalid_polish` → message대로 고쳐 다시 실행한다.
- 결과를 SKILL 6절 형식으로 보여 주고, 훅·제목·썸네일을 고르게 한다. 사용자가 후보를 고치겠다고 하면 반영해 다시 `evaluate`로 점수를 보여 준다.

## 3. 확인 후 반영

고른 것을 한 줄씩 요약하고 "이대로 기획안에 반영할까요?(이전 제목·훅 대본은 `## 패키징` 이력에 남습니다)"를 묻는다. 동의한 뒤에만 실행한다.

선택 JSON `{"title", "hook_script"?, "hook_formula"?, "thumbnail": {"words", "expression", "framing", "background"}}`을 `workspace/tmp/choice.json`에 Write로 쓰고:
`ytc polish apply <draft> --choice workspace/tmp/choice.json --hash <context의 hash>`
- `draft_changed` → "기획안이 그새 바뀌었습니다"라고 알리고 2절부터 다시 한다(파일은 그대로다).
- `warnings`는 그대로 보여 준다. 콘티 경고가 있으면 "콘티의 대사는 자동으로 바뀌지 않았습니다. `/yt-coach:yt-conti <draft> `로 다시 만드세요"라고 안내한다.

## 4. 마무리

```
✅ 기획안 반영 → drafts/2026-10-20-편의점-컵라면-맵기-4단계-도전.md
제목·훅 대본·썸네일 지시서(## 패키징)가 바뀌었습니다. 이전 값은 ## 패키징 이력에 있습니다.
```
