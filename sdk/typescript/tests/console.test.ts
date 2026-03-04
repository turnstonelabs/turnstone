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
