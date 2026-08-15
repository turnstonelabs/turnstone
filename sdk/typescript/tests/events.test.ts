import { describe, expect, it } from "vitest";
import {
  isContentEvent,
  isAgentContextEvent,
  isErrorEvent,
  isStreamEndEvent,
  isToolResultEvent,
  isWsStateEvent,
  isApproveRequestEvent,
  isApprovalResolvedEvent,
  isReasoningEvent,
  isHistoryResyncEvent,
  isUserTurnEvent,
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

  it("isAgentContextEvent", () => {
    const e: ServerEvent = {
      type: "agent_context",
      ws_id: "ws1",
      parent_call_id: "task-A",
      prompt_tokens: 41_000,
      context_window: 128_000,
    };
    expect(isAgentContextEvent(e)).toBe(true);
    if (!isAgentContextEvent(e)) throw new Error("agent context type guard failed");
    expect(e.parent_call_id).toBe("task-A");
    expect(e.context_window).toBe(128_000);
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

  it("carries accepted tool projection metadata", () => {
    const e: ServerEvent = {
      type: "tool_result",
      call_id: "c-final",
      name: "open_preview",
      output: "guarded\nscalar",
      is_error: true,
      preview: { kind: "html", attachment_id: "preview-1" },
      accepted: true,
      effect_status: "unknown",
      _event_id: 42,
    };
    expect(isToolResultEvent(e)).toBe(true);
    if (!isToolResultEvent(e)) throw new Error("tool result type guard failed");
    expect(e.accepted).toBe(true);
    expect(e.preview).toEqual({ kind: "html", attachment_id: "preview-1" });
    expect(e.effect_status).toBe("unknown");
    expect(e._event_id).toBe(42);
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
      persistence_state: "retrying",
    };
    expect(isWsStateEvent(e)).toBe(true);
  });

  it("isApproveRequestEvent", () => {
    const e: ServerEvent = { type: "approve_request", items: [] };
    expect(isApproveRequestEvent(e)).toBe(true);
  });

  it("isApprovalResolvedEvent", () => {
    const e: ServerEvent = {
      type: "approval_resolved",
      approved: false,
      feedback: "Approval timed out",
    };
    expect(isApprovalResolvedEvent(e)).toBe(true);
  });

  it("isHistoryResyncEvent", () => {
    const e: ServerEvent = {
      type: "history_resync",
      ws_id: "ws1",
      reason: "handoff_mismatch",
    };
    expect(isHistoryResyncEvent(e)).toBe(true);
    expect(isContentEvent(e)).toBe(false);
  });

  it("isUserTurnEvent", () => {
    const e: ServerEvent = {
      type: "user_turn",
      content: "hello",
      sender: "user-1",
      client_send_ids: ["browser-send"],
      _event_id: 17,
    };
    expect(isUserTurnEvent(e)).toBe(true);
    expect(isContentEvent(e)).toBe(false);
  });
});
