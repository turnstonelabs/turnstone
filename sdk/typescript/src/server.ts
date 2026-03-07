import { BaseClient, type ClientOptions } from "./base.js";
import type { ServerEvent } from "./events.js";
import type {
  AuthLoginResponse,
  AuthSetupResponse,
  AuthStatusResponse,
  CreateWorkstreamRequest,
  CreateWorkstreamResponse,
  DashboardResponse,
  HealthResponse,
  ListSavedWorkstreamsResponse,
  ListWorkstreamsResponse,
  SendAndWaitOptions,
  SendResponse,
  StatusResponse,
  TurnResult,
} from "./types.js";

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
    return this.request("POST", "/v1/api/workstreams/new", { json: opts });
  }

  async closeWorkstream(wsId: string): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/workstreams/close", {
      json: { ws_id: wsId },
    });
  }

  // -- Chat interaction -----------------------------------------------------

  async send(message: string, wsId: string): Promise<SendResponse> {
    return this.request("POST", "/v1/api/send", {
      json: { message, ws_id: wsId },
    });
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
