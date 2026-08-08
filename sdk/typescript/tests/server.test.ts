import { describe, expect, it, vi } from "vitest";
import { TurnstoneServer } from "../src/server.js";
import { TurnstoneAPIError } from "../src/errors.js";

function mockFetch(response: object, status = 200): typeof globalThis.fetch {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(response), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

function mockFetchError(
  error: object,
  status: number,
): typeof globalThis.fetch {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(error), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

describe("TurnstoneServer", () => {
  it("listWorkstreams returns parsed response", async () => {
    const fetchFn = mockFetch({
      workstreams: [
        {
          ws_id: "ws1",
          name: "test",
          state: "idle",
          kind: "interactive",
          parent_ws_id: null,
          user_id: "u1",
        },
      ],
    });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.listWorkstreams();
    expect(resp.workstreams).toHaveLength(1);
    // Row key renamed id → ws_id in the Stage 2 list-verb lift.
    expect(resp.workstreams[0].ws_id).toBe("ws1");
    expect(resp.workstreams[0].kind).toBe("interactive");
    expect(fetchFn).toHaveBeenCalledWith(
      "http://test/v1/api/workstreams",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("createWorkstream sends correct body", async () => {
    const fetchFn = mockFetch({ ws_id: "ws_new", name: "Analysis" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.createWorkstream({
      name: "Analysis",
      judge_model: "judge-fast",
      client_type: "scheduled",
      notify_targets: [{ channel_type: "slack", channel_id: "C123" }],
    });
    expect(resp.ws_id).toBe("ws_new");

    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({
      name: "Analysis",
      judge_model: "judge-fast",
      client_type: "scheduled",
      notify_targets: [{ channel_type: "slack", channel_id: "C123" }],
    });
  });

  it("send posts correct payload", async () => {
    const fetchFn = mockFetch({ status: "ok" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.send("Hello", "ws1");

    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/workstreams/ws1/send");
    expect(JSON.parse(init.body)).toEqual({ message: "Hello" });
  });

  it("approve selects a cycle without duplicating ws_id in the body", async () => {
    const fetchFn = mockFetch({ status: "ok", cycle_id: "cycle-1" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const response = await client.approve({
      wsId: "ws1",
      approved: false,
      cycleId: "cycle-1",
      callId: "call-1",
    });

    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/workstreams/ws1/approve");
    expect(JSON.parse(init.body)).toEqual({
      approved: false,
      cycle_id: "cycle-1",
      call_id: "call-1",
    });
    expect(response.cycle_id).toBe("cycle-1");
  });

  it("cancel preserves the dropped-work snapshot", async () => {
    const fetchFn = mockFetch({
      status: "cancelled",
      dropped: { tool_calls: ["call-1"] },
    });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const response = await client.cancel("ws1", { force: true });

    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ force: true });
    expect(response.dropped).toEqual({ tool_calls: ["call-1"] });
  });

  it("injects auth header when token provided", async () => {
    const fetchFn = mockFetch({ workstreams: [] });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      token: "tok_abc",
      fetch: fetchFn,
    });
    await client.listWorkstreams();

    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Authorization).toBe("Bearer tok_abc");
  });

  it("throws TurnstoneAPIError on 404", async () => {
    const fetchFn = mockFetchError({ error: "Not found" }, 404);
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await expect(client.send("hi", "bad_ws")).rejects.toThrow(
      TurnstoneAPIError,
    );
    try {
      await client.send("hi", "bad_ws");
    } catch (e) {
      expect(e).toBeInstanceOf(TurnstoneAPIError);
      expect((e as TurnstoneAPIError).statusCode).toBe(404);
    }
  });

  it("health returns parsed response", async () => {
    const fetchFn = mockFetch({
      status: "ok",
      version: "0.3.0",
      uptime_seconds: 120,
    });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.health();
    expect(resp.status).toBe("ok");
    expect(resp.version).toBe("0.3.0");
  });
});
