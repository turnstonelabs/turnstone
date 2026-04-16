import { BaseClient, type ClientOptions } from "./base.js";
import type { ServerEvent } from "./events.js";
import type {
  AttachmentContent,
  AttachmentUpload,
  AuthLoginResponse,
  AuthSetupResponse,
  AuthStatusResponse,
  CreateWorkstreamRequest,
  CreateWorkstreamResponse,
  DashboardResponse,
  DeleteMemoryOptions,
  HealthResponse,
  ListAttachmentsResponse,
  ListMemoriesOptions,
  ListMemoriesResponse,
  ListSavedWorkstreamsResponse,
  ListWorkstreamsResponse,
  MemoryInfo,
  SaveMemoryRequest,
  SearchMemoriesRequest,
  SendAndWaitOptions,
  SendResponse,
  SkillSummary,
  StatusResponse,
  TurnResult,
  UploadAttachmentResponse,
} from "./types.js";

function generateWsId(): string {
  // 16 bytes => 32 hex chars; matches `secrets.token_hex(16)` server-side.
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

function attachmentToBlob(att: AttachmentUpload): Blob {
  if (att.data instanceof Blob) {
    return att.mimeType
      ? new Blob([att.data], { type: att.mimeType })
      : att.data;
  }
  // Copy bytes into a fresh ArrayBuffer-backed Uint8Array. The Blob
  // BlobPart type rejects ArrayBufferLike views (could be backed by
  // SharedArrayBuffer); a freshly allocated buffer is plainly ArrayBuffer.
  const src = att.data;
  const fresh = new Uint8Array(new ArrayBuffer(src.byteLength));
  fresh.set(src);
  return new Blob([fresh], {
    type: att.mimeType ?? "application/octet-stream",
  });
}

/** Async client for the turnstone server API. */
export class TurnstoneServer extends BaseClient {
  constructor(options: ClientOptions) {
    super(options);
  }

  // -- Workstream management ------------------------------------------------

  async listWorkstreams(): Promise<ListWorkstreamsResponse> {
    return this.request("GET", "/v1/api/workstreams");
  }

  async dashboard(): Promise<DashboardResponse> {
    return this.request("GET", "/v1/api/dashboard");
  }

  async createWorkstream(
    opts?: CreateWorkstreamRequest,
  ): Promise<CreateWorkstreamResponse> {
    const attachments = opts?.attachments;
    if (attachments && attachments.length > 0) {
      // Multipart variant: pre-generate ws_id so cluster routers can
      // hash to the owning node before this body lands. Server accepts
      // either a server-generated id (when meta.ws_id is empty) or the
      // caller-supplied one.
      const meta: Record<string, unknown> = { ...opts };
      delete (meta as { attachments?: unknown }).attachments;
      if (!meta.ws_id) {
        meta.ws_id = generateWsId();
      }
      const form = new FormData();
      form.append("meta", JSON.stringify(meta));
      for (const att of attachments) {
        form.append("file", attachmentToBlob(att), att.filename);
      }
      return this.request("POST", "/v1/api/workstreams/new", { form });
    }
    return this.request("POST", "/v1/api/workstreams/new", {
      json: opts ?? {},
    });
  }

  async closeWorkstream(wsId: string): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/workstreams/close", {
      json: { ws_id: wsId },
    });
  }

  // -- Chat interaction -----------------------------------------------------

  async send(
    message: string,
    wsId: string,
    opts?: { attachmentIds?: string[] },
  ): Promise<SendResponse> {
    const body: Record<string, unknown> = { message, ws_id: wsId };
    if (opts?.attachmentIds !== undefined) {
      body.attachment_ids = opts.attachmentIds;
    }
    return this.request("POST", "/v1/api/send", { json: body });
  }

  // -- Attachments ----------------------------------------------------------

  async uploadAttachment(
    wsId: string,
    file: AttachmentUpload,
  ): Promise<UploadAttachmentResponse> {
    const form = new FormData();
    form.append("file", attachmentToBlob(file), file.filename);
    return this.request("POST", `/v1/api/workstreams/${wsId}/attachments`, {
      form,
    });
  }

  async listAttachments(wsId: string): Promise<ListAttachmentsResponse> {
    return this.request("GET", `/v1/api/workstreams/${wsId}/attachments`);
  }

  async getAttachmentContent(
    wsId: string,
    attachmentId: string,
  ): Promise<AttachmentContent> {
    return this.requestBytes(
      "GET",
      `/v1/api/workstreams/${wsId}/attachments/${attachmentId}/content`,
    );
  }

  async deleteAttachment(
    wsId: string,
    attachmentId: string,
  ): Promise<StatusResponse> {
    return this.request(
      "DELETE",
      `/v1/api/workstreams/${wsId}/attachments/${attachmentId}`,
    );
  }

  async approve(opts: {
    wsId: string;
    approved?: boolean;
    feedback?: string | null;
    always?: boolean;
  }): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/approve", {
      json: {
        ws_id: opts.wsId,
        approved: opts.approved ?? true,
        feedback: opts.feedback,
        always: opts.always,
      },
    });
  }

  async planFeedback(opts: {
    wsId: string;
    feedback?: string;
  }): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/plan", {
      json: { ws_id: opts.wsId, feedback: opts.feedback ?? "" },
    });
  }

  async command(opts: {
    wsId: string;
    command: string;
  }): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/command", {
      json: { ws_id: opts.wsId, command: opts.command },
    });
  }

  async cancel(
    wsId: string,
    opts?: { force?: boolean },
  ): Promise<StatusResponse> {
    const body: Record<string, unknown> = { ws_id: wsId };
    if (opts?.force) body.force = true;
    return this.request("POST", "/v1/api/cancel", { json: body });
  }

  // -- Streaming ------------------------------------------------------------

  async *streamEvents(wsId: string): AsyncIterableIterator<ServerEvent> {
    yield* this.streamSSE<ServerEvent>("/v1/api/events", { ws_id: wsId });
  }

  async *streamGlobalEvents(): AsyncIterableIterator<ServerEvent> {
    yield* this.streamSSE<ServerEvent>("/v1/api/events/global");
  }

  // -- High-level convenience -----------------------------------------------

  async sendAndWait(
    message: string,
    wsId: string,
    opts?: SendAndWaitOptions,
  ): Promise<TurnResult> {
    const result: TurnResult = {
      wsId,
      contentParts: [],
      reasoningParts: [],
      toolResults: [],
      errors: [],
      timedOut: false,
      get content() {
        return this.contentParts.join("");
      },
      get reasoning() {
        return this.reasoningParts.join("");
      },
      get ok() {
        return !this.timedOut && this.errors.length === 0;
      },
    };

    // Open SSE stream BEFORE sending to avoid missing early events
    const timeoutMs = opts?.timeout ?? 600_000;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
      // Start consuming the per-workstream SSE stream first
      const events = this.streamSSE<ServerEvent>(
        "/v1/api/events",
        { ws_id: wsId },
        controller.signal,
      );

      const sendResp = await this.send(message, wsId);
      if (sendResp.status === "busy") {
        result.errors.push("Workstream is busy");
        return result;
      }

      for await (const event of events) {
        opts?.onEvent?.(event);

        switch (event.type) {
          case "content":
            result.contentParts.push(event.text);
            break;
          case "reasoning":
            result.reasoningParts.push(event.text);
            break;
          case "tool_result":
            result.toolResults.push({
              name: event.name,
              output: event.output,
            });
            break;
          case "error":
            result.errors.push(event.message);
            break;
          case "ws_state":
            if (event.state === "idle") return result;
            break;
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        result.timedOut = true;
      } else {
        throw err;
      }
    } finally {
      clearTimeout(timer);
      controller.abort();
    }

    return result;
  }

  // -- Saved workstreams ----------------------------------------------------

  async listSavedWorkstreams(): Promise<ListSavedWorkstreamsResponse> {
    return this.request("GET", "/v1/api/workstreams/saved");
  }

  // -- Skills -----------------------------------------------------------------

  async listSkills(): Promise<SkillSummary[]> {
    const resp = await this.request<{ skills: SkillSummary[] }>(
      "GET",
      "/v1/api/skills",
    );
    return resp.skills;
  }

  // -- Memories -------------------------------------------------------------

  async listMemories(
    opts?: ListMemoriesOptions,
  ): Promise<ListMemoriesResponse> {
    const params: Record<string, string | number> = {};
    if (opts?.type) params.type = opts.type;
    if (opts?.scope) params.scope = opts.scope;
    if (opts?.scope_id) params.scope_id = opts.scope_id;
    if (opts?.limit !== undefined) params.limit = opts.limit;
    return this.request("GET", "/v1/api/memories", { params });
  }

  async saveMemory(opts: SaveMemoryRequest): Promise<MemoryInfo> {
    return this.request("POST", "/v1/api/memories", { json: opts });
  }

  async searchMemories(
    opts: SearchMemoriesRequest,
  ): Promise<ListMemoriesResponse> {
    return this.request("POST", "/v1/api/memories/search", { json: opts });
  }

  async deleteMemory(
    name: string,
    opts?: DeleteMemoryOptions,
  ): Promise<StatusResponse> {
    const params: Record<string, string> = {};
    if (opts?.scope) params.scope = opts.scope;
    if (opts?.scope_id) params.scope_id = opts.scope_id;
    return this.request("DELETE", `/v1/api/memories/${name}`, { params });
  }

  // -- Auth -----------------------------------------------------------------

  async login(opts: {
    token?: string;
    username?: string;
    password?: string;
  }): Promise<AuthLoginResponse> {
    const body =
      opts.username && opts.password
        ? { username: opts.username, password: opts.password }
        : { token: opts.token ?? "" };
    return this.request("POST", "/v1/api/auth/login", { json: body });
  }

  async authStatus(): Promise<AuthStatusResponse> {
    return this.request("GET", "/v1/api/auth/status");
  }

  async setup(opts: {
    username: string;
    displayName: string;
    password: string;
  }): Promise<AuthSetupResponse> {
    return this.request("POST", "/v1/api/auth/setup", {
      json: {
        username: opts.username,
        display_name: opts.displayName,
        password: opts.password,
      },
    });
  }

  async logout(): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/auth/logout");
  }

  // -- Health ---------------------------------------------------------------

  async health(): Promise<HealthResponse> {
    return this.request("GET", "/health");
  }
}
