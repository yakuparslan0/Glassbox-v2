import { create } from "zustand";
import type { ViewState } from "../types/model";
import { usePlayback } from "./playbackStore";
import { getRunData, stateClient, useRunStore } from "./runStore";

interface ViewStore {
  state: ViewState | null;
  prevView: ViewState | null; // state at step - 1 (null at the first step), for diff highlighting
  step: number;
}

/** The program state at the current step, rebuilt by the Web Worker. */
export const useViewStore = create<ViewStore>(() => ({ state: null, prevView: null, step: 0 }));

/** Wire playback -> worker -> view store (call once at startup). */
export function initViewSync(): void {
  const refresh = (): void => {
    const step = usePlayback.getState().step;
    if (step < 1) {
      useViewStore.setState({ state: null, prevView: null, step: 0 });
      return;
    }
    const data = getRunData();
    // the previous step is a side request: it must not supersede (and drop) the current step's answer
    const prev = step > 1 ? stateClient.stateAtSeq(data.endSeqOfStep(step - 1), false) : Promise.resolve(null);
    void Promise.all([stateClient.stateAtSeq(data.endSeqOfStep(step)), prev]).then(([state, prevView]) => {
      if (state && usePlayback.getState().step === step) useViewStore.setState({ state, prevView, step });
    });
  };
  usePlayback.subscribe((s, prev) => s.step !== prev.step && refresh());
  // a finished run can change the end-of-step seq of the last step (trailing output/keyframe events)
  useRunStore.subscribe((s, prev) => s.status === "finished" && prev.status !== "finished" && refresh());
}
