import { Config } from "@remotion/cli/config";

// PNG frames: no JPEG chroma bleed at the white/black edges against the green key
Config.setVideoImageFormat("png");
Config.setCodec("h264");
