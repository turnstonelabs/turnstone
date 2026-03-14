import { BaseClient, type ClientOptions } from "./base.js";
import type { ClusterEvent } from "./events.js";
import type {
  AdminListMemoriesOptions,
  AdminMemoryInfo,
  AdminSearchMemoriesOptions,
  AuditQueryOptions,
  AuditResponse,
  AuthLoginResponse,
  AuthSetupResponse,
  AuthStatusResponse,
  ClusterNodesResponse,
  ClusterOverviewResponse,
  ClusterSnapshotResponse,
  ClusterWorkstreamsResponse,
  ConsoleCreateWsRequest,
  ConsoleCreateWsResponse,
  ConsoleHealthResponse,
  CreatePolicyOptions,
  CreateRoleOptions,
  CreateScheduleRequest,
  CreateTemplateOptions,
  CreateWsTemplateOptions,
  ListAdminMemoriesResponse,
  ListScheduleRunsResponse,
  ListSchedulesResponse,
  ListSettingSchemaResponse,
  ListSettingsResponse,
  NodeDetailResponse,
  NodesOptions,
  OrgInfo,
  PromptTemplateInfo,
  RoleInfo,
  ScheduleInfo,
  SettingInfo,
  StatusResponse,
  ToolPolicyInfo,
  UpdateOrgOptions,
  UpdatePolicyOptions,
  UpdateRoleOptions,
  UpdateScheduleRequest,
  UpdateSettingOptions,
  UpdateTemplateOptions,
  UpdateWsTemplateOptions,
  UsageQueryOptions,
  UsageResponse,
  UserRoleInfo,
  WorkstreamsOptions,
  WsTemplateInfo,
  WsTemplateVersionInfo,
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

  async snapshot(): Promise<ClusterSnapshotResponse> {
    return this.request("GET", "/v1/api/cluster/snapshot");
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

  // -- Schedules ------------------------------------------------------------

  async listSchedules(): Promise<ListSchedulesResponse> {
    return this.request("GET", "/v1/api/admin/schedules");
  }

  async createSchedule(opts: CreateScheduleRequest): Promise<ScheduleInfo> {
    return this.request("POST", "/v1/api/admin/schedules", { json: opts });
  }

  async getSchedule(taskId: string): Promise<ScheduleInfo> {
    return this.request("GET", `/v1/api/admin/schedules/${taskId}`);
  }

  async updateSchedule(
    taskId: string,
    opts: UpdateScheduleRequest,
  ): Promise<ScheduleInfo> {
    return this.request("PUT", `/v1/api/admin/schedules/${taskId}`, {
      json: opts,
    });
  }

  async deleteSchedule(taskId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/schedules/${taskId}`);
  }

  async listScheduleRuns(
    taskId: string,
    opts?: { limit?: number },
  ): Promise<ListScheduleRunsResponse> {
    return this.request("GET", `/v1/api/admin/schedules/${taskId}/runs`, {
      params: { limit: opts?.limit ?? 50 },
    });
  }

  // -- Governance: Roles ------------------------------------------------------

  async listRoles(): Promise<{ roles: RoleInfo[] }> {
    return this.request("GET", "/v1/api/admin/roles");
  }

  async createRole(opts: CreateRoleOptions): Promise<RoleInfo> {
    return this.request("POST", "/v1/api/admin/roles", { json: opts });
  }

  async updateRole(roleId: string, opts: UpdateRoleOptions): Promise<RoleInfo> {
    return this.request("PUT", `/v1/api/admin/roles/${roleId}`, {
      json: opts,
    });
  }

  async deleteRole(roleId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/roles/${roleId}`);
  }

  async listUserRoles(userId: string): Promise<{ roles: UserRoleInfo[] }> {
    return this.request("GET", `/v1/api/admin/users/${userId}/roles`);
  }

  async assignRole(userId: string, roleId: string): Promise<StatusResponse> {
    return this.request("POST", `/v1/api/admin/users/${userId}/roles`, {
      json: { role_id: roleId },
    });
  }

  async unassignRole(userId: string, roleId: string): Promise<StatusResponse> {
    return this.request(
      "DELETE",
      `/v1/api/admin/users/${userId}/roles/${roleId}`,
    );
  }

  // -- Governance: Organizations ----------------------------------------------

  async listOrgs(): Promise<{ orgs: OrgInfo[] }> {
    return this.request("GET", "/v1/api/admin/orgs");
  }

  async getOrg(orgId: string): Promise<OrgInfo> {
    return this.request("GET", `/v1/api/admin/orgs/${orgId}`);
  }

  async updateOrg(orgId: string, opts: UpdateOrgOptions): Promise<OrgInfo> {
    return this.request("PUT", `/v1/api/admin/orgs/${orgId}`, { json: opts });
  }

  // -- Governance: Tool Policies ----------------------------------------------

  async listPolicies(): Promise<{ policies: ToolPolicyInfo[] }> {
    return this.request("GET", "/v1/api/admin/policies");
  }

  async createPolicy(opts: CreatePolicyOptions): Promise<ToolPolicyInfo> {
    return this.request("POST", "/v1/api/admin/policies", { json: opts });
  }

  async updatePolicy(
    policyId: string,
    opts: UpdatePolicyOptions,
  ): Promise<ToolPolicyInfo> {
    return this.request("PUT", `/v1/api/admin/policies/${policyId}`, {
      json: opts,
    });
  }

  async deletePolicy(policyId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/policies/${policyId}`);
  }

  // -- Governance: Prompt Templates -------------------------------------------

  async listTemplates(): Promise<{ templates: PromptTemplateInfo[] }> {
    return this.request("GET", "/v1/api/admin/templates");
  }

  async createTemplate(
    opts: CreateTemplateOptions,
  ): Promise<PromptTemplateInfo> {
    return this.request("POST", "/v1/api/admin/templates", { json: opts });
  }

  async updateTemplate(
    templateId: string,
    opts: UpdateTemplateOptions,
  ): Promise<PromptTemplateInfo> {
    return this.request("PUT", `/v1/api/admin/templates/${templateId}`, {
      json: opts,
    });
  }

  async deleteTemplate(templateId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/templates/${templateId}`);
  }

  // -- Governance: Workstream Templates ----------------------------------------

  async listWsTemplates(): Promise<WsTemplateInfo[]> {
    const data = await this.request<{ ws_templates: WsTemplateInfo[] }>(
      "GET",
      "/v1/api/admin/ws-templates",
    );
    return data.ws_templates || [];
  }

  async createWsTemplate(
    opts: CreateWsTemplateOptions,
  ): Promise<WsTemplateInfo> {
    return this.request("POST", "/v1/api/admin/ws-templates", {
      json: opts,
    });
  }

  async getWsTemplate(wsTemplateId: string): Promise<WsTemplateInfo> {
    return this.request("GET", `/v1/api/admin/ws-templates/${wsTemplateId}`);
  }

  async updateWsTemplate(
    wsTemplateId: string,
    opts: UpdateWsTemplateOptions,
  ): Promise<WsTemplateInfo> {
    return this.request("PUT", `/v1/api/admin/ws-templates/${wsTemplateId}`, {
      json: opts,
    });
  }

  async deleteWsTemplate(wsTemplateId: string): Promise<void> {
    await this.request("DELETE", `/v1/api/admin/ws-templates/${wsTemplateId}`);
  }

  async listWsTemplateVersions(
    wsTemplateId: string,
  ): Promise<WsTemplateVersionInfo[]> {
    const data = await this.request<{ versions: WsTemplateVersionInfo[] }>(
      "GET",
      `/v1/api/admin/ws-templates/${wsTemplateId}/versions`,
    );
    return data.versions || [];
  }

  // -- Governance: Usage & Audit ----------------------------------------------

  async getUsage(opts: UsageQueryOptions): Promise<UsageResponse> {
    const params: Record<string, string> = { since: opts.since };
    if (opts.until) params.until = opts.until;
    if (opts.user_id) params.user_id = opts.user_id;
    if (opts.model) params.model = opts.model;
    if (opts.group_by) params.group_by = opts.group_by;
    return this.request("GET", "/v1/api/admin/usage", { params });
  }

  async getAudit(opts?: AuditQueryOptions): Promise<AuditResponse> {
    const params: Record<string, string> = {};
    if (opts?.action) params.action = opts.action;
    if (opts?.user_id) params.user_id = opts.user_id;
    if (opts?.since) params.since = opts.since;
    if (opts?.until) params.until = opts.until;
    if (opts?.limit !== undefined) params.limit = String(opts.limit);
    if (opts?.offset !== undefined) params.offset = String(opts.offset);
    return this.request("GET", "/v1/api/admin/audit", { params });
  }

  // -- Admin: Memories ------------------------------------------------------

  async listMemories(
    opts?: AdminListMemoriesOptions,
  ): Promise<ListAdminMemoriesResponse> {
    const params: Record<string, string | number> = {};
    if (opts?.type) params.type = opts.type;
    if (opts?.scope) params.scope = opts.scope;
    if (opts?.scope_id) params.scope_id = opts.scope_id;
    if (opts?.limit !== undefined) params.limit = opts.limit;
    return this.request("GET", "/v1/api/admin/memories", { params });
  }

  async searchMemories(
    opts: AdminSearchMemoriesOptions,
  ): Promise<ListAdminMemoriesResponse> {
    const params: Record<string, string | number> = { q: opts.q };
    if (opts.type) params.type = opts.type;
    if (opts.scope) params.scope = opts.scope;
    if (opts.scope_id) params.scope_id = opts.scope_id;
    if (opts.limit !== undefined) params.limit = opts.limit;
    return this.request("GET", "/v1/api/admin/memories/search", { params });
  }

  async getMemory(memoryId: string): Promise<AdminMemoryInfo> {
    return this.request("GET", `/v1/api/admin/memories/${memoryId}`);
  }

  async deleteMemory(memoryId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/memories/${memoryId}`);
  }

  // -- System: Settings -------------------------------------------------------

  async listSettings(): Promise<ListSettingsResponse> {
    return this.request("GET", "/v1/api/admin/settings");
  }

  async getSettingsSchema(): Promise<ListSettingSchemaResponse> {
    return this.request("GET", "/v1/api/admin/settings/schema");
  }

  async updateSetting(
    key: string,
    opts: UpdateSettingOptions,
  ): Promise<SettingInfo> {
    return this.request("PUT", `/v1/api/admin/settings/${key}`, {
      json: opts,
    });
  }

  async deleteSetting(key: string, nodeId?: string): Promise<StatusResponse> {
    const params: Record<string, string> = {};
    if (nodeId) params.node_id = nodeId;
    return this.request("DELETE", `/v1/api/admin/settings/${key}`, {
      params,
    });
  }
}
