---
description: 기획안(draft)으로 전체 대사와 컷별 가이드가 들어간 콘티·스토리보드를 만든다
argument-hint: '<draft 파일 | latest | 콘티 파일(-conti.md)>'
---
저장된 기획안을 장면 단위 콘티로 바꾸고, 도식과 레퍼런스 참고 사진이 붙은 HTML 스토리보드를 만든다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
콘티 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-conti/SKILL.md` — 만들기 전에 읽는다
인자: `$ARGUMENTS`

## 1. 인자

- 인자가 `-conti.md`로 끝나면 **다시 그리기**다: `ytc conti render <파일>` → 4절로 간다(사용자가 프런트매터를 고친 경우).
  사용자가 "반복 컷을 합쳐 줘", "장면으로 합치기"라고 하면 4-1절로 간다.
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
장면 12개 · 참고 사진: 장면 내용에 맞춰 고른 사진은 "참고", 맞는 사진이 없으면 같은 샷 크기의 사진("구도만 참고")입니다.
고치고 싶으면 -conti.md의 script·cuts를 직접 수정하고 /yt-coach:yt-conti <그 파일> 로 다시 그리세요.
```
- 결과의 `abs_html` 경로를 함께 보여 준다.

## 4-1. 반복 컷 장면으로 합치기

예전 콘티처럼 한 섹션 안에 같은 장면(`action`)이 여러 번 나오면 장면 하나로 합친다. 대사는 바꾸지 않고 나눌 자리만 정한다.

1. 계획: `ytc conti merge <파일>` → `{hash, before, after, sections, warnings}`
   - `nothing_to_merge`("합칠 장면이 없어요")면 그대로 알리고 멈춘다.
   - `warnings`의 `scene_too_long`은 message를 그대로 전한다(합친 장면이 길다).
2. 나누기: `${CLAUDE_PLUGIN_ROOT}/skills/yt-conti/merge.md` 규칙대로 `sections` 중 `split: true`인 섹션만 장면 순서(`order`)와
   대사 나눌 자리(`lines`)를 정한다. 대사 글자·문장부호는 바꾸지 않는다.
3. 미리보기: 섹션마다 "장면(action) — 대사"를 순서대로 보여 주고 "컷 {before}개 → 장면 {after}개로 합칠까요?"라고 묻는다.
   **저장 전 사용자 확인**을 받는다. 고칠 곳을 말하면 반영해 다시 보여 준다.
4. 저장: 나누기 결과(`{"sections": [...]}`)를 `workspace/tmp/merge.json`에 Write로 쓰고
   `ytc conti merge <파일> --cuts workspace/tmp/merge.json --hash <1의 hash>`
   - `invalid_merge` → message대로 고쳐 다시 실행한다. `conti_changed` → 1부터 다시 한다(그사이 콘티가 바뀜).
   - 합치기 전 상태는 이력(`before_merge`)에 남아 화면의 이력에서 되돌릴 수 있다고 알린다.
   - 합친 장면은 묶음 첫 컷의 참고 사진을 이어받는다. 장면에 더 맞는 사진은 화면의 "사진 다시 고르기"로 고른다
     (CLI에는 사진 고르기 명령이 없다).

## 5. 자막 SRT 내보내기

사용자가 CapCut에 자막을 넣고 싶다고 하면(저장 직후 안내해도 좋다) 저장된 `-conti.md`로 실행한다.

```
python -m scripts.conti srt <conti.md> [--track caption|line]
```
- 기본은 컷별 `caption` → `drafts/<기획안>-captions.srt`. `--track line`이면 컷별 대사 → `-lines.srt`.
- 시간은 콘티 컷 길이(`sec`)를 이어 붙인 값이라 실제 편집과 어긋날 수 있다. CapCut PC의 캡션 → 자막 불러오기에서 파일을 고른 뒤 자막 칸을 끌어 맞추도록 안내한다.
- 비어 있는 컷은 건너뛰고, 같은 자막이 이어지면 하나로 합친다. 자막이 하나도 없으면 파일을 만들지 않는다(`path: null`).
- 콘티 md·html은 바꾸지 않는다. SRT는 매번 콘티에서 다시 만드는 파일이라 같은 이름이 있으면 덮어쓴다(손으로 고친 SRT는 다른 이름으로 저장하라고 안내).

### 편집본 영상에 시간 맞추기 (`--video`)

자막이 없는 편집본(CapCut 컷 편집을 끝낸 영상)이 있으면, 그 영상의 실제 컷 경계에 자막 시간을 맞출 수 있다.

```
python -m scripts.conti srt <conti.md> --video <편집본 영상 경로> [--track caption|line]
```
- 영상 경로는 사용자가 붙여넣은 그대로 받는다(앞뒤 공백·따옴표는 지운다). 영상 파일은 읽기만 하고 바꾸거나 복사하지 않는다.
- 영상의 컷을 찾아(`retime.detected`) 콘티 컷 경계에 가까운 컷에 붙이고(`retime.snapped`/`inner`), 못 맞춘 경계는 길이 비율로 둔다.
- `retime.warnings`가 있으면 그대로 전한다(컷을 못 찾음, 콘티 길이와 영상 길이가 많이 다름 → 다른 영상인지 확인).
- 결과는 기존 SRT와 같은 이름의 파일을 덮어쓴다. 화면에서는 `POST /api/conti/srt-fit` 작업으로 같은 일을 한다.

## 6. 움직이는 자막 영상 (선택 설치, Remotion)

CapCut 위 트랙에 얹어 쓸 "초록 배경 + 움직이는 자막" MP4가 필요하다고 하면, 저장된 `-conti.md`의 자막(`caption`)으로 만든다. SRT와 같은 시간 계산을 쓴다.

```
python -m scripts.captions check
python -m scripts.captions setup        # 처음 한 번, 사용자가 직접 (Node 18 이상, remotion/에서 npm ci)
python -m scripts.captions render <conti.md> [--video <편집본 영상 경로>] [--track caption|line]
```
- `remotion_missing`이 나오면 자동으로 설치하지 말고 `ytc captions setup`을 사용자에게 안내한다. 첫 렌더 때 Remotion이 Chrome Headless Shell을 내려받아 오래 걸릴 수 있다고 알린다.
- 결과는 `drafts/<기획안>-captions.mp4`(H.264, 30fps, 초록 `#00FF00` 배경, 같은 이름이면 교체). 크기는 쇼츠 1080×1920, 롱폼 1920×1080, `--video`를 주면 그 영상의 비율(짧은 변 1080)과 실제 컷에 맞춘 시간을 따른다. 영상 파일은 읽기만 한다.
- 자막이 하나도 없으면 `no_captions`("내보낼 자막이 없습니다"), 렌더가 실패하면 `render_failed`(마지막 출력 몇 줄 포함)이며 기존 mp4는 그대로 둔다.
- CapCut: 이 영상을 맨 위 트랙에 얹고 크로마키로 초록을 빼도록 안내한다. 화면에서는 `POST /api/conti/caption-video` 작업(진행률 "N/M 프레임")으로 같은 일을 한다.
