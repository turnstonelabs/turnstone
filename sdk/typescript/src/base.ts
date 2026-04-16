import { TurnstoneAPIError } from "./errors.js";
import { parseSSEStream } from "./sse.js";

export interface TlsOptions {
  /** Path to CA certificate PEM file (Node.js only). */
  caCert?: string;
  /** Path to client certificate PEM file for mTLS (Node.js only). */
  clientCert?: string;
  /** Path to client key PEM file for mTLS (Node.js only). */
  clientKey?: string;
}

export interface ClientOptions {
  /** Server base URL (e.g. "http://localhost:8080"). */
  baseUrl: string;
  /** Bearer token for authentication. */
  token?: string;
  /** Custom fetch implementation (defaults to globalThis.fetch). */
  fetch?: typeof globalThis.fetch;
  /**
   * TLS certificate paths for documentation and tooling.
   * The SDK does not read these directly — pass a custom `fetch`
   * configured with your runtime's TLS agent (e.g. Node.js https.Agent).
   * See docs/tls.md for examples.
   */
  tls?: TlsOptions;
}

export interface RequestOptions {
  json?: object;
  params?: Record<string, string | number>;
  /**
   * When set, send as multipart form-data with this body. The runtime's
   * fetch sets the Content-Type + boundary itself, so we deliberately do
   * not include a Content-Type header in this case.
   */
  form?: FormData;
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
    const headers: Record<string, string> = {};
    if (!options?.form) {
      headers["Content-Type"] = "application/json";
    }
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const url = this._buildUrl(path, options?.params);

    let body: BodyInit | undefined;
    if (options?.form) {
      body = options.form;
    } else if (options?.json) {
      body = JSON.stringify(options.json);
    }

    const resp = await this.fetchFn(url, {
      method,
      headers,
      body,
    });

    if (!resp.ok) {
      let msg = "";
      try {
        const errBody = (await resp.json()) as Record<string, unknown>;
        msg = (errBody.error as string) ?? (errBody.detail as string) ?? "";
      } catch {
        msg = await resp.text().catch(() => "");
      }
      throw new TurnstoneAPIError(resp.status, msg || `HTTP ${resp.status}`);
    }

    return (await resp.json()) as T;
  }

  protected async requestBytes(
    method: string,
    path: string,
    options?: { params?: Record<string, string | number> },
  ): Promise<{ bytes: Uint8Array; contentType: string; filename: string }> {
    const headers: Record<string, string> = {};
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const url = this._buildUrl(path, options?.params);
    const resp = await this.fetchFn(url, { method, headers });
    if (!resp.ok) {
      let msg = "";
      try {
        const errBody = (await resp.json()) as Record<string, unknown>;
        msg = (errBody.error as string) ?? (errBody.detail as string) ?? "";
      } catch {
        msg = await resp.text().catch(() => "");
      }
      throw new TurnstoneAPIError(resp.status, msg || `HTTP ${resp.status}`);
    }
    const contentType =
      resp.headers.get("content-type") ?? "application/octet-stream";
    const disposition = resp.headers.get("content-disposition") ?? "";
    const match = /filename="?([^";]+)"?/.exec(disposition);
    const filename = match ? match[1] : "";
    const buf = await resp.arrayBuffer();
    return { bytes: new Uint8Array(buf), contentType, filename };
  }

  private _buildUrl(
    path: string,
    params?: Record<string, string | number>,
  ): string {
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
    return url;
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
