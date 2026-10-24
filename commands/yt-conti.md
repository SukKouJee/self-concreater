---
description: 기획안(draft)으로 전체 대사와 컷별 가이드가 들어간 콘티·스토리보드를 만든다
argument-hint: '<draft 파일 | latest | 콘티 파일(-conti.md)>'
---
저장된 기획안을 컷 단위 콘티로 바꾸고, 도식과 레퍼런스 참고 프레임이 붙은 HTML 스토리보드를 만든다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
콘티 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-conti/SKILL.md` — 만들기 전에 읽는다
인자: `$ARGUMENTS`

## 1. 인자

- 인자가 `-conti.md`로 끝나면 **다시 그리기**다: `ytc conti render <파일>` → 4절로 간다(사용자가 프런트매터를 고친 경우).
- `latest`이거나 비어 있으면 `drafts/`에서 `-conti`가 아닌 가장 최근 md를 고르고, 무엇을 골랐는지 한 줄로 알린다. drafts가 비어 있으면 `/yt-coach:yt-draft`를 먼저 안내하고 멈춘다.
- 그 외에는 draft 파일 경로(`drafts/…md`).

## 2. 만들기

`ytc conti context <draft>` →
- `conti_exists`가 true면 "이미 콘티가 있습니다. 다시 만들까요?(내 메모는 남습니다)"를 먼저 묻는다.
- `speech_basis`가 0이면 "레퍼런스 자막이 없어 말 속도를 기본값(초당 4.5자)으로 잡았습니다"라고 알린다.
- 규칙대로 conti JSON을 만들고 SKILL 6절 형식으로 보여 준 뒤 확인을 받는다. 고칠 곳을 말하면 반영해 다시 보여 준다.

## 3. 저장

conti JSON을 `workspace/tmp/conti.json`에 Write로 쓰고 `ytc conti save <draft> --conti workspace/tmp/conti.json` (다시 만들기면 `--force`)
- `invalid_conti` → message대로 고쳐 다시 실행한다.
- `warnings`:
  - `line_too_long`: 해당 컷 번호와 message를 보여 주고 "대사를 줄이거나 다음 컷으로 넘길까요?"를 묻는다. 동의하면 고쳐서 `--force`로 다시 저장한다.
  - `script_mismatch`: 컷 대사와 전체 대사를 맞춰(대개 전체 대사를 컷 대사 기준으로) `--force`로 다시 저장한다. 사용자에게 묻지 않아도 된다.

## 4. 마무리

```
✅ 콘티 저장 → drafts/2026-10-20-편의점-컵라면-맵기-4단계-도전-conti.md
🖼 스토리보드 → drafts/2026-10-20-편의점-컵라면-맵기-4단계-도전-conti.html (브라우저로 열기·인쇄)
컷 36개 · 참고 프레임은 같은 샷 크기의 레퍼런스 장면입니다(구도만 참고).
고치고 싶으면 -conti.md의 script·cuts를 직접 수정하고 /yt-coach:yt-conti <그 파일> 로 다시 그리세요.
```
- 결과의 `abs_html` 경로를 함께 보여 준다.

## 5. 자막 SRT 내보내기

사용자가 CapCut에 자막을 넣고 싶다고 하면(저장 직후 안내해도 좋다) 저장된 `-conti.md`로 실행한다.

```
python -m scripts.conti srt <conti.md> [--track caption|line]
```
- 기본은 컷별 `caption` → `drafts/<기획안>-captions.srt`. `--track line`이면 컷별 대사 → `-lines.srt`.
- 시간은 콘티 컷 길이(`sec`)를 이어 붙인 값이라 실제 편집과 어긋날 수 있다. CapCut PC의 캡션 → 자막 불러오기에서 파일을 고른 뒤 자막 칸을 끌어 맞추도록 안내한다.
- 비어 있는 컷은 건너뛰고, 같은 자막이 이어지면 하나로 합친다. 자막이 하나도 없으면 파일을 만들지 않는다(`path: null`).
- 콘티 md·html은 바꾸지 않는다. SRT는 매번 콘티에서 다시 만드는 파일이라 같은 이름이 있으면 덮어쓴다(손으로 고친 SRT는 다른 이름으로 저장하라고 안내).
