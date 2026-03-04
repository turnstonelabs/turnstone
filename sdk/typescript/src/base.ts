import { TurnstoneAPIError } from "./errors.js";
import { parseSSEStream } from "./sse.js";

export interface ClientOptions {
  /** Server base URL (e.g. "http://localhost:8080"). */
  baseUrl: string;
  /** Bearer token for authentication. */
  token?: string;
  /** Custom fetch implementation (defaults to globalThis.fetch). */
  fetch?: typeof globalThis.fetch;
}

export interface RequestOptions {
  json?: object;
  params?: Record<string, string | number>;
}

export class BaseClient {
  protected readonly baseUrl: string;
  protected readonly token: string;
  protected readonly fetchFn: typeof globalThis.fetch;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.token = options.token ?? "";
    this.fetchFn = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  protected async request<T>(
    method: string,
    path: string,
    options?: RequestOptions,
  ): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    let url = `${this.baseUrl}${path}`;
    if (options?.params) {
      const searchParams = new URLSearchParams();
      for (const [key, value] of Object.entries(options.params)) {
        if (value !== undefined && value !== "") {
          searchParams.set(key, String(value));
        }
      }
      const qs = searchParams.toString();
      if (qs) url += `?${qs}`;
    }

    const resp = await this.fetchFn(url, {
      method,
      headers,
      body: options?.json ? JSON.stringify(options.json) : undefined,
    });

    if (!resp.ok) {
      let msg = "";
      try {
        const body = (await resp.json()) as Record<string, unknown>;
        msg = (body.error as string) ?? (body.detail as string) ?? "";
      } catch {
        msg = await resp.text().catch(() => "");
      }
      throw new TurnstoneAPIError(resp.status, msg || `HTTP ${resp.status}`);
    }

    return (await resp.json()) as T;
  }

  protected async *streamSSE<T = Record<string, unknown>>(
    path: string,
    params?: Record<string, string | number>,
    signal?: AbortSignal,
  ): AsyncIterableIterator<T> {
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
    };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    let url = `${this.baseUrl}${path}`;
    if (params) {
      const searchParams = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== "") {
          searchParams.set(key, String(value));
        }
      }
      const qs = searchParams.toString();
      if (qs) url += `?${qs}`;
    }

    const resp = await this.fetchFn(url, { method: "GET", headers, signal });
    if (!resp.ok) {
      throw new TurnstoneAPIError(
        resp.status,
        `SSE connection failed: HTTP ${resp.status}`,
      );
    }

    yield* parseSSEStream<T>(resp);
  }
}
