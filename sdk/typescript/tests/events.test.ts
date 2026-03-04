import { describe, expect, it } from "vitest";
import {
  isContentEvent,
  isErrorEvent,
  isStreamEndEvent,
  isToolResultEvent,
  isWsStateEvent,
  isApproveRequestEvent,
  isPlanReviewEvent,
  isReasoningEvent,
} from "../src/events.js";
import type { ServerEvent } from "../src/events.js";

describe("event type guards", () => {
  it("isContentEvent", () => {
    const e: ServerEvent = { type: "content", text: "hello" };
    expect(isContentEvent(e)).toBe(true);
    expect(isErrorEvent(e)).toBe(false);
  });

  it("isReasoningEvent", () => {
    const e: ServerEvent = { type: "reasoning", text: "step 1" };
    expect(isReasoningEvent(e)).toBe(true);
    expect(isContentEvent(e)).toBe(false);
  });

  it("isErrorEvent", () => {
    const e: ServerEvent = { type: "error", message: "bad" };
    expect(isErrorEvent(e)).toBe(true);
  });

  it("isStreamEndEvent", () => {
    const e: ServerEvent = { type: "stream_end" };
    expect(isStreamEndEvent(e)).toBe(true);
  });

  it("isToolResultEvent", () => {
    const e: ServerEvent = {
      type: "tool_result",
      call_id: "c1",
      name: "search",
      output: "found",
    };
    expect(isToolResultEvent(e)).toBe(true);
  });

  it("isWsStateEvent", () => {
    const e: ServerEvent = {
      type: "ws_state",
      ws_id: "ws1",
      state: "idle",
      tokens: 0,
      context_ratio: 0,
      activity: "",
      activity_state: "",
    };
    expect(isWsStateEvent(e)).toBe(true);
  });

  it("isApproveRequestEvent", () => {
    const e: ServerEvent = { type: "approve_request", items: [] };
    expect(isApproveRequestEvent(e)).toBe(true);
  });

  it("isPlanReviewEvent", () => {
    const e: ServerEvent = { type: "plan_review", content: "## Plan" };
    expect(isPlanReviewEvent(e)).toBe(true);
  });
});
