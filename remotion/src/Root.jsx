import React from "react";
import { Composition } from "remotion";
import { Captions } from "./Captions.jsx";

const defaultProps = {
  fps: 30,
  width: 1080,
  height: 1920,
  durationInFrames: 90,
  background: "#00FF00",
  captions: [],
};

export const Root = () => (
  <Composition
    id="Captions"
    component={Captions}
    fps={defaultProps.fps}
    width={defaultProps.width}
    height={defaultProps.height}
    durationInFrames={defaultProps.durationInFrames}
    defaultProps={defaultProps}
    calculateMetadata={({ props }) => ({
      fps: props.fps,
      width: props.width,
      height: props.height,
      durationInFrames: props.durationInFrames,
    })}
  />
);
