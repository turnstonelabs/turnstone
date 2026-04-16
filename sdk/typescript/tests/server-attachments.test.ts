import { describe, expect, it, vi } from "vitest";
import { TurnstoneServer } from "../src/server.js";

function mockFetch(response: object, status = 200): typeof globalThis.fetch {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(response), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

function mockFetchBytes(
  body: Uint8Array,
  contentType: string,
  filename = "",
): typeof globalThis.fetch {
  const headers: Record<string, string> = { "content-type": contentType };
  if (filename)
    headers["content-disposition"] = `inline; filename="${filename}"`;
  return vi
    .fn()
    .mockResolvedValue(new Response(body, { status: 200, headers }));
}

describe("TurnstoneServer attachments", () => {
  it("uploadAttachment sends multipart with filename", async () => {
    const fetchFn = mockFetch({
      attachment_id: "att-1",
      filename: "a.txt",
      mime_type: "text/plain",
      size_bytes: 5,
      kind: "text",
    });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const data = new TextEncoder().encode("hello");
    const result = await client.uploadAttachment("ws-X", {
      filename: "a.txt",
      data,
      mimeType: "text/plain",
    });
    expect(result.attachment_id).toBe("att-1");

    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/workstreams/ws-X/attachments");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    // Browser/Node fetch sets the Content-Type header from FormData itself
    expect(init.headers["Content-Type"]).toBeUndefined();
  });

  it("listAttachments hits the GET endpoint", async () => {
    const fetchFn = mockFetch({ attachments: [] });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.listAttachments("ws-X");
    expect(resp.attachments).toEqual([]);
    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/workstreams/ws-X/attachments");
    expect(init.method).toBe("GET");
  });

  it("getAttachmentContent returns raw bytes + parsed headers", async () => {
    const bytes = new TextEncoder().encode("hello world");
    const fetchFn = mockFetchBytes(
      bytes,
      "text/plain; charset=utf-8",
      "notes.md",
    );
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const result = await client.getAttachmentContent("ws-X", "att-1");
    expect(new TextDecoder().decode(result.bytes)).toBe("hello world");
    expect(result.contentType).toBe("text/plain; charset=utf-8");
    expect(result.filename).toBe("notes.md");
  });

  it("deleteAttachment hits the DELETE endpoint", async () => {
    const fetchFn = mockFetch({ status: "deleted" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const resp = await client.deleteAttachment("ws-X", "att-1");
    expect(resp.status).toBe("deleted");
    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.method).toBe("DELETE");
  });

  it("send threads attachment_ids when provided", async () => {
    const fetchFn = mockFetch({ status: "ok" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.send("hi", "ws-X", { attachmentIds: ["a1", "a2"] });
    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({
      message: "hi",
      ws_id: "ws-X",
      attachment_ids: ["a1", "a2"],
    });
  });

  it("send omits attachment_ids when not supplied", async () => {
    const fetchFn = mockFetch({ status: "ok" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.send("hi", "ws-X");
    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ message: "hi", ws_id: "ws-X" });
  });

  it("createWorkstream with attachments sends multipart and auto-generates ws_id", async () => {
    const fetchFn = mockFetch({
      ws_id: "00ff00000000000000000000000000ff",
      name: "demo",
      attachment_ids: ["att-1"],
    });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    const data = new TextEncoder().encode("hello");
    const resp = await client.createWorkstream({
      name: "demo",
      initial_message: "describe",
      attachments: [{ filename: "a.txt", data, mimeType: "text/plain" }],
    });
    expect(resp.attachment_ids).toEqual(["att-1"]);

    const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://test/v1/api/workstreams/new");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);

    const form = init.body as FormData;
    const meta = JSON.parse(form.get("meta") as string);
    expect(meta.name).toBe("demo");
    expect(meta.initial_message).toBe("describe");
    expect(meta.ws_id).toMatch(/^[0-9a-f]{32}$/);
    expect(meta.attachments).toBeUndefined();

    const file = form.get("file");
    expect(file).toBeInstanceOf(Blob);
  });

  it("createWorkstream without attachments uses JSON body", async () => {
    const fetchFn = mockFetch({ ws_id: "ws-json", name: "j" });
    const client = new TurnstoneServer({
      baseUrl: "http://test",
      fetch: fetchFn,
    });
    await client.createWorkstream({ name: "j" });
    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ name: "j" });
  });
});
