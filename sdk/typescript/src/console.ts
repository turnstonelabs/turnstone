import { BaseClient, type ClientOptions } from "./base.js";
import type { ClusterEvent } from "./events.js";
import type {
  AuthLoginResponse,
  AuthSetupResponse,
  AuthStatusResponse,
  ClusterNodesResponse,
  ClusterOverviewResponse,
  ClusterWorkstreamsResponse,
  ConsoleCreateWsRequest,
  ConsoleCreateWsResponse,
  ConsoleHealthResponse,
  NodeDetailResponse,
  NodesOptions,
  StatusResponse,
  WorkstreamsOptions,
} from "./types.js";

/** Async client for the turnstone console API. */
export class TurnstoneConsole extends BaseClient {
  constructor(options: ClientOptions) {
    super(options);
  }

  // -- Cluster overview -----------------------------------------------------

  async overview(): Promise<ClusterOverviewResponse> {
    return this.request("GET", "/v1/api/cluster/overview");
  }

  async nodes(opts?: NodesOptions): Promise<ClusterNodesResponse> {
    return this.request("GET", "/v1/api/cluster/nodes", {
      params: {
        sort: opts?.sort ?? "activity",
        limit: opts?.limit ?? 100,
        offset: opts?.offset ?? 0,
      },
    });
  }

  async workstreams(
    opts?: WorkstreamsOptions,
  ): Promise<ClusterWorkstreamsResponse> {
    const params: Record<string, string | number> = {
      sort: opts?.sort ?? "state",
      page: opts?.page ?? 1,
      per_page: opts?.per_page ?? 50,
    };
    if (opts?.state) params.state = opts.state;
    if (opts?.node) params.node = opts.node;
    if (opts?.search) params.search = opts.search;
    return this.request("GET", "/v1/api/cluster/workstreams", { params });
  }

  async nodeDetail(nodeId: string): Promise<NodeDetailResponse> {
    return this.request("GET", `/v1/api/cluster/node/${nodeId}`);
  }

  async createWorkstream(
    opts?: ConsoleCreateWsRequest,
  ): Promise<ConsoleCreateWsResponse> {
    return this.request("POST", "/v1/api/cluster/workstreams/new", {
      json: opts,
    });
  }

  // -- Streaming ------------------------------------------------------------

  async *clusterEvents(): AsyncIterableIterator<ClusterEvent> {
    yield* this.streamSSE<ClusterEvent>("/v1/api/cluster/events");
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

  async health(): Promise<ConsoleHealthResponse> {
    return this.request("GET", "/health");
  }
}
