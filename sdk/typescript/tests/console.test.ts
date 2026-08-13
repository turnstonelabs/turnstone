import { describe, expect, it, vi } from "vitest";
import { TurnstoneConsole } from "../src/console.js";

function mockFetch(response: object): typeof globalThis.fetch {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(response), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
}

describe("TurnstoneConsole", () => {
  it("updates memory hooks and reads index health", async () => {
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            memory_id: "m1",
            name: "deployment_process",
            description: "Production deployment workflow",
            type: "general",
            scope: "global",
            scope_id: "",
            content: "Deploy from main",
            created: "2026-08-11T00:00:00",
            updated: "2026-08-11T00:00:00",
            last_accessed: "",
            access_count: 0,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            budget_chars: 65536,
            over_budget: false,
            max_char_count: 120,
            max_entry_count: 2,
            over_by_chars: 0,
            invalid_description_count: 0,
            envelope_count: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });

    await client.updateMemoryDescription(
      "m1",
      "  Production\n deployment workflow  ",
    );
    const health = await client.memoryIndexHealth();

    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/admin/memories/m1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({
      description: "Production deployment workflow",
    });
    expect(health.budget_chars).toBe(65536);
  });

  it("overview returns parsed response", async () => {
    const fetchFn = mockFetch({
      nodes: 2,
      workstreams: 5,
      states: { idle: 5 },
      aggregate: { total_tokens: 1000, total_tool_calls: 0 },
      version_drift: false,
      versions: ["0.3.0"],
    });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.overview();
    expect(resp.nodes).toBe(2);
    expect(resp.workstreams).toBe(5);
  });

  it("nodes passes query parameters", async () => {
    const fetchFn = mockFetch({ nodes: [], total: 0 });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.nodes({ sort: "tokens", limit: 50, offset: 10 });

    const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("sort=tokens");
    expect(url).toContain("limit=50");
    expect(url).toContain("offset=10");
  });

  it("workstreams passes filter parameters", async () => {
    const fetchFn = mockFetch({
      workstreams: [],
      total: 0,
      page: 1,
      per_page: 50,
      pages: 0,
    });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.workstreams({ state: "running", page: 2 });

    const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("state=running");
    expect(url).toContain("page=2");
  });

  it("createWorkstream sends the live cluster-create contract", async () => {
    const fetchFn = mockFetch({
      status: "ok",
      correlation_id: "ws-new",
      target_node: "node-a",
    });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.createWorkstream({
      node_id: "node-a",
      project_id: "project-42",
      judge_model: "judge-fast",
    });

    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({
      node_id: "node-a",
      project_id: "project-42",
      judge_model: "judge-fast",
    });
  });

  it("routeCreateWorkstream returns placement metadata", async () => {
    const fetchFn = mockFetch({
      ws_id: "ws-new",
      name: "routed",
      node_url: "http://node-a:8080",
      node_id: "node-a",
      routing_strategy: "target_node",
    });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const response = await client.routeCreateWorkstream({
      name: "routed",
      target_node: "node-a",
      client_type: "scheduled",
      notify_targets: [{ channel_type: "slack", channel_id: "C123" }],
    });

    expect(response.node_id).toBe("node-a");
    expect(response.routing_strategy).toBe("target_node");
    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toMatchObject({
      client_type: "scheduled",
      notify_targets: [{ channel_type: "slack", channel_id: "C123" }],
    });
  });

  it("routeCreateWorkstream rejects attachments + target_node", async () => {
    const fetchFn = vi.fn().mockResolvedValue(
      new Response("{}", {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const data = new TextEncoder().encode("hi");
    await expect(
      client.routeCreateWorkstream({
        name: "x",
        target_node: "n1",
        attachments: [{ filename: "a.txt", data }],
      }),
    ).rejects.toThrow(/target_node/);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("routeWorkstreamLive returns the non-mutating liveness probe", async () => {
    const fetchFn = mockFetch({ ws_id: "saved/ws", live: true });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });

    const response = await client.routeWorkstreamLive("saved/ws");

    expect(response).toEqual({ ws_id: "saved/ws", live: true });
    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain("/v1/api/route/workstreams/saved%2Fws/live");
    expect(init.method).toBe("GET");
  });

  it("health returns parsed response", async () => {
    const fetchFn = mockFetch({
      status: "ok",
      service: "turnstone-console",
      nodes: 2,
      workstreams: 5,
      version_drift: false,
      versions: ["0.3.0"],
    });
    const client = new TurnstoneConsole({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.health();
    expect(resp.status).toBe("ok");
    expect(resp.nodes).toBe(2);
  });
});
