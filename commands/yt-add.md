---
description: 레퍼런스(또는 내 영상) 링크를 분석해 영상별 md를 만든다
argument-hint: '<category> [ref|mine] <links...> [--force] [--refetch]'
---
링크를 하나씩 수집·분석해 `refs/ref_<category>/videos/<id>.md`를 만든다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
분석 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-analyze/SKILL.md` — **첫 영상을 분석하기 전에 한 번 읽는다**
인자: `$ARGUMENTS`

## 1. 인자 해석

- 첫 단어 = category. 다음 단어가 `ref` 또는 `mine`이면 role(기본 `ref`). 나머지 = 링크들(공백·줄바꿈 구분).
- `--force`: 이미 분석한 영상도 **다시 분석**한다. 수집 데이터(자막·컷)는 캐시를 쓴다. `## 내 메모`는 유지된다.
- `--refetch`: 자막·컷까지 **다시 수집**한 뒤 분석한다(자막 요청이 늘어 차단 위험이 있으니 꼭 필요할 때만).
- category가 없으면 `ytc refstore categories`로 목록을 보여 주고 고르게 한다. 없는 카테고리면 `/yt-coach:yt-init`이나 `ytc refstore init <category>`를 제안한다.
- 링크가 없으면 링크를 달라고 한다. 채널·재생목록을 자동으로 긁어 오지 않는다(링크는 사용자가 직접 고른다).

## 2. 영상마다 순서대로 (한 번에 하나씩)

진행 상황을 `[2/5] <제목 앞부분>` 처럼 짧게 알린다.
- `--force`: fetch에 `--reanalyze`, videomd write에 `--force`를 붙인다(cuts는 그대로 캐시).
- `--refetch`: fetch·cuts에 `--force`, videomd write에 `--force`를 붙인다.

**a. 수집** `ytc fetch <링크> --category <cat> --role <role>`
- `status: exists` → "이미 분석됨, 건너뜀" 후 다음 영상.
- `error: blocked` / `proxy_failed` → message를 그대로 전하고 **남은 영상 전부 중단**(계속 요청하면 차단이 길어진다).
- `error: no_transcript` → "자막이 없습니다. 음성 인식(STT)으로 만들까요, 화면만 보고 분석할까요?"라고 묻는다.
  STT를 고르면 `ytc stt <id>` (stt_unavailable이면 설치 안내를 전하고 화면 분석으로 진행).
- `role_conflict`, `invalid_url`, `private`, `unavailable` 등 → 이유를 한 줄로 알리고 다음 영상.
- `sparse: true` → 말이 거의 없는 영상이다. 분석 때 화면을 본다(스킬 0단계).

**b. 컷 통계** `ytc cuts <id>`
- 실패하면 이유를 한 줄 알리고 컷 통계 없이 계속한다(pacing·camera가 비게 된다).

**c. 분석 입력** `ytc videomd context <id> --category <cat>`

**d. 분석** yt-analyze 규칙대로 분석 JSON을 만든다(examples가 있으면 먼저 읽는다).

**e. 확인** 저장 전에 아래 형식으로 보여 주고 묻는다: "저장할까요? (네 / 고칠 점 / 이후 모두 저장)"
```
📼 <제목> (<길이>초, <쇼츠|롱폼>, 자막 <cc|auto|stt|없음>)
한 줄: <summary>
훅: <hook_type> · 서사: <narrative_type> · 평균 컷 <avg_cut_sec>초
섹션: hook 0–2.5 → body/story 2.5–48 → outro 48–55 → cta 55–60
기법: 결과 선공개(훅), 고정 정면 + 점프컷(촬영)
```
- 고칠 점을 말하면 반영해 다시 보여 준다.
- "이후 모두 저장"이면 남은 영상은 확인 없이 저장하고 마지막에 한꺼번에 보고한다.

**f. 저장** 분석 JSON을 `workspace/tmp/analysis_<id>.json`에 Write로 쓰고
`ytc videomd write <id> --category <cat> --role <role> --analysis workspace/tmp/analysis_<id>.json`
- `invalid_analysis` → message대로 JSON을 고쳐 다시 실행한다(사용자에게 묻지 않는다).
- `status: exists` → 이미 있음(`--force` 없이 실행된 경우).

## 3. 마무리 보고

```
✅ 3개 저장 · 1개 건너뜀 · 1개 실패
- 저장: <제목> → refs/ref_<cat>/videos/<id>.md
- 건너뜀: <제목> (이미 분석됨)
- 실패: <링크> (비공개 영상)
기법 카탈로그: 새 기법 4개, 기존 기법에 출처 2개 추가 → refs/ref_<cat>/techniques.md

<마지막 write 결과의 stage_text>
```
그리고 한 줄 덧붙인다: "분석이 틀린 곳은 md를 직접 고치고 `reviewed: true`로 바꿔 두세요. 다음 분석부터 그 방식을 따릅니다."
