---
name: yt-typify
description: yt-coach에서 cluster run 결과(축별 판정·군집·아키타입 후보·이상치)를 해석해 근거 문장과 아키타입 이름을 만든다. /yt-typify가 사용한다.
---

# yt-typify — 유형화 결과 해석 규칙

입력은 `ytc cluster run <category>`의 JSON(`workspace/typify/<cat>.json`)이다.
수치(군집, 실루엣, 점유율, 판정)는 스크립트가 이미 냈다. **여기서는 숫자를 바꾸지 않고 해석만 한다.**
출력은 `ytc cluster apply`에 넘길 notes JSON 하나다.

## 1. 축 (설계서 5.2)

| 축 | 무엇을 봤나 | 군집 요약(summary)에서 볼 것 |
|---|---|---|
| structure | 섹션 순서·라벨별 시간 비중·길이 | skeleton, mean_ratio, mean_duration |
| hook | 훅 방식 | hook_type 개수, hook_sec |
| narrative | 서사 방식 | narrative_type 개수 |
| title | 제목 패턴(숫자·질문·괄호·감탄) | pattern, examples |
| pacing | 평균 컷 길이, 훅·본론 컷 길이 | avg_cut_sec, by_section |

판정 뜻
- `typable`: 뚜렷한 갈래가 있고 큰 갈래가 충분히 크다 → 따라 할 '형식'이 있다
- `weak`: 경향은 보이나 흩어져 있다 → 참고만
- `untypable`: 공통 형식이 없다 → 이 축은 자유롭게 해도 된다(기법 모티브의 재료)
- `insufficient`: 그 축의 값이 있는 영상이 5개 미만(예: 컷 통계 없음)

## 2. 근거 문장 (축마다 1~2문장)

- **숫자와 영상 수로 말한다.** "12개 중 8개가 …", "평균 컷 1.4초 묶음(9개)과 4초대 묶음(3개)"
- 큰 군집의 특징을 summary에서 그대로 옮긴다. 없는 사실을 만들지 않는다.
- untypable이면 왜 흩어졌는지(어떤 값들로 나뉘었는지)를 쓴다.
- `status: preliminary`(ref 5~9개)면 문장 끝을 "~로 보인다", "~경향이 있다"로 쓴다. 단정하지 않는다.
- 한국어, 표 한 칸에 들어갈 길이(80자 안팎).

예
- structure (typable): "12개 중 8개가 '짧은 훅 → 긴 이야기형 본론 → 구독 요청' 골격이고, 본론이 평균 80%를 차지한다."
- narrative (untypable): "리스트 4 · 여정 3 · 반응 3 · 비교 2로 갈려 공통 서사는 없다."
- pacing (weak): "평균 컷 1~2초 묶음이 7개로 가장 크지만 4초 이상 영상도 5개 섞여 있다."

## 3. 아키타입 이름

`archetypes`의 각 후보(A1, A2 …)에 이름을 하나씩 제안한다.
- `prev_name`이 있으면 **그 이름을 그대로 제안**한다(구성 영상이 절반 넘게 같은 묶음). 바꿀 이유가 뚜렷할 때만 새 이름을 함께 제안.
- 새 이름 규칙: 6~14자, 골격·훅·분위기가 보이게. 영상 제목·채널명을 그대로 쓰지 않는다.
  좋음: "결과 먼저 몰아먹기", "사이즈별 리스트 비교", "도전 설정 중계형"
  나쁨: "A1형", "먹방 유형 1", "또솔 스타일"
- 이름은 제안일 뿐이다. 사용자가 확정한다.

## 4. 사용자에게 보여 줄 형식

```
🧭 mukbang 유형화 — 정식 판정 (ref 12개)

| 축 | 판정 | 근거 |
|---|---|---|
| structure | typable | 12개 중 8개가 … |
| hook | typable | … |
| narrative | untypable | … |
| title | weak | … |
| pacing | typable | … |

아키타입 후보
A1 (8개) 골격 hook → body/story → cta · 평균 컷 1.4초 · 대표: <제목>
   → 이름 제안: "결과 먼저 몰아먹기"  (이전 이름 그대로)
A2 (3개) 골격 hook → intro → body/list_item → outro · 평균 컷 4.1초 · 대표: <제목>
   → 이름 제안: "사이즈별 리스트 비교"

이상치: <제목> (structure, pacing에서 혼자 동떨어짐)
```
그리고 묻는다: "아키타입 이름을 이대로 할까요? 바꿀 이름이 있으면 알려 주세요."

## 5. notes JSON (확정 후 저장)

```json
{
  "axes": {
    "structure": "12개 중 8개가 …",
    "hook": "…", "narrative": "…", "title": "…", "pacing": "…"
  },
  "archetypes": [
    {"id": "A1", "name": "결과 먼저 몰아먹기"},
    {"id": "A2", "name": "사이즈별 리스트 비교"}
  ]
}
```
- run 결과의 **모든 축**에 근거 문장이 있어야 하고, **모든 아키타입 id**에 이름이 있어야 한다(빠지면 `invalid_notes`).
- 이상치는 스크립트가 표로 쓴다. 따로 적지 않는다.
