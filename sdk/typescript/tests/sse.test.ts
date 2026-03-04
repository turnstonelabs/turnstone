import { describe, expect, it } from "vitest";
import { parseSSEStream } from "../src/sse.js";

function makeSSEResponse(...events: string[]): Response {
  const body = events.map((e) => `data: ${e}\n\n`).join("");
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    headers: { "content-type": "text/event-stream" },
  });
}

describe("parseSSEStream", () => {
  it("yields parsed JSON from SSE data lines", async () => {
    const resp = makeSSEResponse(
      '{"type": "content", "text": "hello"}',
      '{"type": "stream_end"}',
    );
    const events: unknown[] = [];
    for await (const event of parseSSEStream(resp)) {
      events.push(event);
    }
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({ type: "content", text: "hello" });
    expect(events[1]).toEqual({ type: "stream_end" });
  });

  it("skips malformed JSON", async () => {
    const resp = makeSSEResponse(
      "not-json",
      '{"type": "info", "message": "ok"}',
    );
    const events: unknown[] = [];
    for await (const event of parseSSEStream(resp)) {
      events.push(event);
    }
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({ type: "info", message: "ok" });
  });

  it("handles multiple events in sequence", async () => {
    const resp = makeSSEResponse(
      '{"type": "connected", "model": "gpt-5"}',
      '{"type": "content", "text": "a"}',
      '{"type": "content", "text": "b"}',
      '{"type": "status", "total_tokens": 10}',
      '{"type": "stream_end"}',
    );
    const events: unknown[] = [];
    for await (const event of parseSSEStream(resp)) {
      events.push(event);
    }
    expect(events).toHaveLength(5);
    const types = events.map((e) => (e as Record<string, unknown>).type);
    expect(types).toEqual([
      "connected",
      "content",
      "content",
      "status",
      "stream_end",
    ]);
  });
});
