# 자막 영상 (Remotion)

콘티의 화면 자막을 크로마 그린(`#00FF00`) 배경 위 움직이는 자막 영상으로 렌더합니다. CapCut에서 크로마키로 올려 쓰세요.

- 필요: Node 18 이상. 처음 한 번 `remotion/`에서 `npm ci` (`ytc captions setup`).
- 첫 렌더 때 Remotion이 Chrome Headless Shell을 내려받습니다(시간이 조금 걸립니다).
- 샘플 렌더: `npx remotion render src/index.jsx Captions out/sample.mp4 --props=sample-props.json`
- 입력(props): `fps`, `width`, `height`, `durationInFrames`, `background`, `captions[{start, end, text}]` (시간은 초).
- 라이선스: Remotion은 개인·직원 3명 이하·비영리 무료, 그보다 큰 회사는 회사 라이선스가 필요합니다.
