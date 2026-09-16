/* A waveform with one draggable region.
 *
 * The region is the point of this, not the picture. Scoring a whole track
 * averages its clean intro together with its solo, so a candidate that
 * matches neither passage can come out on top. Dragging across the part you
 * actually care about is the cheapest accuracy win in the whole tool, and it
 * only works if choosing the span is as easy as pointing at it.
 *
 * Exactly one region is kept. Two would raise the question of which one the
 * score used, and the answer would have to be "the first", which nobody
 * would guess from looking at it.
 */

import { api } from "./api.js";

const COLOURS = {
  wave: "#4b5663",
  progress: "#f0a020",
  cursor: "#e8edf2",
  region: "rgba(240, 160, 32, 0.22)",
};

export function createWave(container, { onChange } = {}) {
  if (typeof WaveSurfer === "undefined") {
    throw new Error("wavesurfer.js did not load");
  }

  const wave = WaveSurfer.create({
    container,
    height: 84,
    waveColor: COLOURS.wave,
    progressColor: COLOURS.progress,
    cursorColor: COLOURS.cursor,
    normalize: true,
    dragToSeek: true,
  });

  const regions = wave.registerPlugin(WaveSurfer.Regions.create());
  regions.enableDragSelection({ color: COLOURS.region });

  let info = null;
  let region = null;
  let loading = null;

  const announce = () => onChange?.(current());

  /** Keep one region: a new drag replaces the old one. */
  regions.on("region-created", (created) => {
    if (region && region !== created) region.remove();
    region = created;
    announce();
  });
  regions.on("region-updated", (updated) => {
    region = updated;
    announce();
  });
  regions.on("region-removed", (removed) => {
    if (region === removed) {
      region = null;
      announce();
    }
  });
  // Stop at the end of the region rather than running on into the next
  // passage, which would defeat the point of having chosen one.
  regions.on("region-out", () => wave.pause());

  function current() {
    if (!region) return null;
    return [Number(region.start.toFixed(3)), Number(region.end.toFixed(3))];
  }

  return {
    element: container,

    /** Name a file to the server, then draw it. Resolves when decoded. */
    async load(path) {
      info = await api.post("/api/match/open", { path });
      regions.clearRegions();
      region = null;
      loading = new Promise((resolve, reject) => {
        wave.once("ready", resolve);
        wave.once("error", reject);
      });
      wave.load(info.url);
      await loading;
      announce();
      return info;
    },

    info: () => info,
    region: current,

    /** Set the region programmatically - used to mirror it in aligned mode. */
    setRegion(span) {
      regions.clearRegions();
      region = null;
      if (span) {
        regions.addRegion({
          start: span[0],
          end: span[1],
          color: COLOURS.region,
          drag: true,
          resize: true,
        });
      } else {
        announce();
      }
    },

    clear() {
      regions.clearRegions();
      region = null;
      announce();
    },

    playPause() {
      if (wave.isPlaying()) {
        wave.pause();
        return;
      }
      if (region) region.play();
      else wave.play();
    },

    stop() {
      if (wave.isPlaying()) wave.pause();
    },

    destroy() {
      wave.destroy();
    },
  };
}

/** "41.20s-58.00s (16.80s)", or a plain statement that nothing is selected. */
export function describeRegion(span) {
  if (!span) return "whole file";
  const [start, end] = span;
  return `${start.toFixed(2)}s-${end.toFixed(2)}s (${(end - start).toFixed(2)}s)`;
}
