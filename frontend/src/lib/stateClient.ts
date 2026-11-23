// Main-thread side of the state worker: structural events are forwarded, requests are latest-wins + LRU cached.
import type { RunEvent } from "../types/generated";
import type { ViewState } from "../types/model";
import type { WorkerIn, WorkerOut } from "../workers/stateWorker";

const STRUCTURAL = new Set(["call", "line", "return", "exception", "keyframe"]);
const CACHE_SIZE = 64;

export class StateClient {
  private worker = new Worker(new URL("../workers/stateWorker.ts", import.meta.url), { type: "module" });
  private nextId = 1;
  private latest = 0;
  private pending = new Map<number, { seq: number; keep: boolean; resolve: (s: ViewState | null) => void }>();
  private cache = new Map<number, ViewState>();

  constructor() {
    this.worker.onmessage = (msg: MessageEvent<WorkerOut>) => {
      const p = this.pending.get(msg.data.id);
      if (!p) return;
      this.pending.delete(msg.data.id);
      if (msg.data.type === "state") {
        this.remember(p.seq, msg.data.state);
        p.resolve(p.keep || msg.data.id === this.latest ? msg.data.state : null); // stale answers are dropped
      } else p.resolve(null);
    };
  }

  reset(): void {
    this.cache.clear();
    for (const p of this.pending.values()) p.resolve(null);
    this.pending.clear();
    this.post({ type: "reset" });
  }

  append(events: readonly RunEvent[]): void {
    const structural = events.filter((e) => STRUCTURAL.has(e.type));
    if (structural.length) this.post({ type: "append", events: structural });
  }

  /** `latestWins = false` makes a side request (e.g. the previous step for diffing) that neither supersedes the
   * main request nor is dropped by it; callers must discard it themselves if it arrives stale. */
  stateAtSeq(seq: number, latestWins = true): Promise<ViewState | null> {
    const hit = this.cache.get(seq);
    if (hit) {
      this.cache.delete(seq);
      this.cache.set(seq, hit);
      return Promise.resolve(hit);
    }
    const id = this.nextId++;
    if (latestWins) this.latest = id;
    return new Promise((resolve) => {
      this.pending.set(id, { seq, keep: !latestWins, resolve });
      this.post({ type: "state", id, seq });
    });
  }

  private remember(seq: number, state: ViewState): void {
    this.cache.set(seq, state);
    if (this.cache.size > CACHE_SIZE) this.cache.delete(this.cache.keys().next().value as number);
  }

  private post(m: WorkerIn): void {
    this.worker.postMessage(m);
  }
}
