---
description: 한 주를 돌아보고 다음 주 방향과 profile 갱신을 제안한다
argument-hint: '[YYYY-Www]'
---
한 주의 미션·레퍼런스·모티브·진단을 돌아보고 다음 주 방향과 프로필 변경을 제안한다. 승인한 것만 `profile.md`에 반영한다. 대화는 한국어로 한다.

스크립트 실행기: `"${CLAUDE_PLUGIN_ROOT}/bin/ytc"` (아래 `ytc`. 실제 호출은 항상 전체 경로로)
회고 규칙: `${CLAUDE_PLUGIN_ROOT}/skills/yt-mission/SKILL.md` 6절
인자: `$ARGUMENTS` (주, 없으면 이번 주)

## 1. 집계

`ytc mission week [YYYY-Www]`
- 미션이 0개면 "이번 주 미션 기록이 없습니다"라고 하고, 레퍼런스·모티브 현황만 짧게 정리한 뒤 다음 주 첫 미션을 권한다.

## 2. 회고 작성과 확인

규칙대로 회고 JSON을 만들어 보여 준다:
```
📅 2026-W43 회고 (10/19 ~ 10/25)
미션 5개 중 4개 제출(80%) · 촬영 3 · 편집 1 · 난이도 적당 2 · 어려움 2
반복된 약점: 손이 화면 밖으로 나감 (2번)
다음 주: 탑뷰 고정 촬영 3번, 컷 편집 1번
프로필 변경 제안:
- 약점 → "- 프레이밍(손 위치)"
- 성장 단계 → 관찰 → 모방  (완료율 80%, 관찰 미션 대부분 적당)
```
회고는 확인 없이 기록해도 된다: `workspace/tmp/review.json`에 쓰고 `ytc mission review-save <week> --review workspace/tmp/review.json`

## 3. 리포트

`ytc refstore categories`의 카테고리마다 `ytc report <category>`를 실행하고, 결과의 `abs_path`를 링크로 준다:
"카테고리별 리포트(판정·아키타입·컷 분포·진단 추이)를 만들었습니다: <abs_path> — 브라우저로 열어 보세요."

## 4. 프로필 반영 (승인 필요)

"프로필 변경 제안을 반영할까요? (전부 / 일부 / 아니오)"
- 승인한 섹션만 JSON으로 `workspace/tmp/profile_updates.json`에 쓰고 `ytc mission profile-update --updates workspace/tmp/profile_updates.json`
- 반영 후: "profile.md를 갱신했습니다. 다음 주 미션부터 반영됩니다."
