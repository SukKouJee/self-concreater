import React from "react";
import { AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig } from "remotion";
import { animate, captionFrames, emphasisTokens, fitFontSize } from "./timing.mjs";

const FONT_FAMILY = '"Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif';
const TEXT_COLOR = "#FFFFFF";
const EMPHASIS_COLOR = "#FFD400";
const OUTLINE_COLOR = "#000000";

const CaptionText = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const { scale, opacity } = animate(frame, durationInFrames, fps);
  const baseSize = Math.min(width, height) * 0.07;
  const maxWidth = width * 0.9;
  const fontSize = fitFontSize(text, baseSize, maxWidth);
  const tokens = emphasisTokens(text);

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: height * 0.18,
      }}
    >
      <div
        style={{
          width: maxWidth,
          maxHeight: fontSize * 1.3 * 2 + fontSize * 0.2,
          overflow: "hidden",
          textAlign: "center",
          fontFamily: FONT_FAMILY,
          fontWeight: 900,
          fontSize,
          lineHeight: 1.3,
          wordBreak: "keep-all",
          overflowWrap: "anywhere",
          paintOrder: "stroke fill",
          WebkitTextStroke: `${fontSize * 0.16}px ${OUTLINE_COLOR}`,
          strokeLinejoin: "round",
          opacity,
          transform: `scale(${scale})`,
          transformOrigin: "50% 100%",
        }}
      >
        {tokens.map((token, i) => (
          <React.Fragment key={`${i}-${token.text}`}>
            {i > 0 ? " " : null}
            <span style={{ color: token.emphasis ? EMPHASIS_COLOR : TEXT_COLOR }}>{token.text}</span>
          </React.Fragment>
        ))}
      </div>
    </AbsoluteFill>
  );
};

export const Captions = ({ background = "#00FF00", captions = [] }) => {
  const { fps } = useVideoConfig();
  const items = captionFrames(captions, fps);
  return (
    <AbsoluteFill style={{ backgroundColor: background }}>
      {items.map((item) => (
        <Sequence key={`${item.from}-${item.text}`} from={item.from} durationInFrames={item.duration} layout="none">
          <CaptionText text={item.text} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
