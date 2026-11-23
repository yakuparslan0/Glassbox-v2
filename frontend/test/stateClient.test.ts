import { afterEach, describe, expect, it, vi } from "vitest";
import type { ViewState } from "../src/types/model";

// Minimal Worker stand-in: records posted requests; the test answers them in any order.
class FakeWorker {
  static last: FakeWorker;
  onmessage: ((m: MessageEvent) => void) | null = null;
  posted: { type: string; id?: number; seq?: number }[] = [];
  constructor() {
    FakeWorker.last = this;
  }
  postMessage(m: { type: string; id?: number; seq?: number }): void {
    this.posted.push(m);
  }
  answer(id: number, state: ViewState): void {
    this.onmessage?.({ data: { type: "state", id, state } } as MessageEvent);
  }
}

const st = (n: number): ViewState => ({ stack: [], globals: { n: { k: "prim", t: "int", r: String(n) } as never }, heap: {} });

describe("StateClient latest-wins with side requests", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("a side request (previous step) neither drops nor is dropped by the main request", async () => {
    vi.stubGlobal("Worker", FakeWorker);
    const { StateClient } = await import("../src/lib/stateClient");
    const client = new StateClient();
    const w = FakeWorker.last;
    const main = client.stateAtSeq(10);
    const side = client.stateAtSeq(9, false);
    const [mainReq, sideReq] = w.posted;
    w.answer(sideReq.id as number, st(9));
    w.answer(mainReq.id as number, st(10));
    expect(await main).toEqual(st(10));
    expect(await side).toEqual(st(9));
  });

  it("a newer main request still supersedes an older one", async () => {
    vi.stubGlobal("Worker", FakeWorker);
    const { StateClient } = await import("../src/lib/stateClient");
    const client = new StateClient();
    const w = FakeWorker.last;
    const old = client.stateAtSeq(5);
    const cur = client.stateAtSeq(6);
    w.answer(w.posted[0].id as number, st(5));
    w.answer(w.posted[1].id as number, st(6));
    expect(await old).toBeNull();
    expect(await cur).toEqual(st(6));
  });
});
