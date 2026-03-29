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
  CreateMcpServerRequest,
  CreatePolicyOptions,
  CreateRoleOptions,
  CreateScheduleRequest,
  CreateSkillRequest,
  CreateSkillResourceRequest,
  ImportMcpConfigResponse,
  ListAdminMemoriesResponse,
  ListMcpServersResponse,
  ListScheduleRunsResponse,
  ListSchedulesResponse,
  ListSettingSchemaResponse,
  ListSettingsResponse,
  ListSkillResourcesResponse,
  ListSkillsResponse,
  McpServerDetail,
  RegistryInstallRequest,
  RegistrySearchResponse,
  SkillDiscoverResponse,
  SkillInfo,
  SkillInstallRequest,
  SkillInstallResponse,
  SkillResourceInfo,
  NodeDetailResponse,
  NodesOptions,
  OrgInfo,
  RoleInfo,
  ScheduleInfo,
  DeleteSettingResponse,
  SettingInfo,
  StatusResponse,
  ToolPolicyInfo,
  UpdateMcpServerRequest,
  UpdateOrgOptions,
  UpdatePolicyOptions,
  UpdateRoleOptions,
  UpdateScheduleRequest,
  UpdateSettingOptions,
  UpdateSkillRequest,
  UsageQueryOptions,
  UsageResponse,
  UserRoleInfo,
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

  // -- Governance: Skills -------------------------------------------------------

  async listSkills(): Promise<SkillInfo[]> {
    const resp = await this.request<ListSkillsResponse>(
      "GET",
      "/v1/api/admin/skills",
    );
    return resp.skills;
  }

  async createSkill(body: CreateSkillRequest): Promise<SkillInfo> {
    return this.request("POST", "/v1/api/admin/skills", { json: body });
  }

  async updateSkill(
    skillId: string,
    body: UpdateSkillRequest,
  ): Promise<SkillInfo> {
    return this.request("PUT", `/v1/api/admin/skills/${skillId}`, {
      json: body,
    });
  }

  async deleteSkill(skillId: string): Promise<void> {
    await this.request("DELETE", `/v1/api/admin/skills/${skillId}`);
  }

  async listSkillResources(skillId: string): Promise<SkillResourceInfo[]> {
    const resp = await this.request<ListSkillResourcesResponse>(
      "GET",
      `/v1/api/admin/skills/${skillId}/resources`,
    );
    return resp.resources;
  }

  async createSkillResource(
    skillId: string,
    body: CreateSkillResourceRequest,
  ): Promise<SkillResourceInfo> {
    return this.request("POST", `/v1/api/admin/skills/${skillId}/resources`, {
      json: body,
    });
  }

  async deleteSkillResource(skillId: string, path: string): Promise<void> {
    await this.request(
      "DELETE",
      `/v1/api/admin/skills/${skillId}/resources/${path.split("/").map(encodeURIComponent).join("/")}`,
    );
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

  async deleteSetting(
    key: string,
    nodeId?: string,
  ): Promise<DeleteSettingResponse> {
    const params: Record<string, string> = {};
    if (nodeId) params.node_id = nodeId;
    return this.request("DELETE", `/v1/api/admin/settings/${key}`, {
      params,
    });
  }

  // -- MCP servers ----------------------------------------------------------

  async listMcpServers(opts?: {
    reveal?: boolean;
  }): Promise<ListMcpServersResponse> {
    const params: Record<string, string> = {};
    if (opts?.reveal) params.reveal = "true";
    return this.request("GET", "/v1/api/admin/mcp-servers", { params });
  }

  async createMcpServer(
    body: CreateMcpServerRequest,
  ): Promise<McpServerDetail> {
    return this.request("POST", "/v1/api/admin/mcp-servers", { json: body });
  }

  async getMcpServer(serverId: string): Promise<McpServerDetail> {
    return this.request("GET", `/v1/api/admin/mcp-servers/${serverId}`);
  }

  async updateMcpServer(
    serverId: string,
    body: UpdateMcpServerRequest,
  ): Promise<McpServerDetail> {
    return this.request("PUT", `/v1/api/admin/mcp-servers/${serverId}`, {
      json: body,
    });
  }

  async deleteMcpServer(serverId: string): Promise<StatusResponse> {
    return this.request("DELETE", `/v1/api/admin/mcp-servers/${serverId}`);
  }

  async reloadMcpServers(): Promise<StatusResponse> {
    return this.request("POST", "/v1/api/admin/mcp-servers/reload");
  }

  async importMcpConfig(
    config: Record<string, unknown>,
  ): Promise<ImportMcpConfigResponse> {
    return this.request("POST", "/v1/api/admin/mcp-servers/import", {
      json: { config },
    });
  }

  // -- MCP Registry ---------------------------------------------------------

  async searchMcpRegistry(opts?: {
    q?: string;
    limit?: number;
    cursor?: string;
  }): Promise<RegistrySearchResponse> {
    const params: Record<string, string> = {};
    if (opts?.q) params.search = opts.q;
    if (opts?.limit) params.limit = String(opts.limit);
    if (opts?.cursor) params.cursor = opts.cursor;
    return this.request("GET", "/v1/api/admin/mcp-registry/search", {
      params,
    });
  }

  async installFromRegistry(
    body: RegistryInstallRequest,
  ): Promise<McpServerDetail> {
    return this.request("POST", "/v1/api/admin/mcp-registry/install", {
      json: body,
    });
  }

  // -- Skill Discovery ------------------------------------------------------

  async discoverSkills(opts?: {
    q?: string;
    limit?: number;
  }): Promise<SkillDiscoverResponse> {
    const params: Record<string, string> = {};
    if (opts?.q) params.q = opts.q;
    if (opts?.limit) params.limit = String(opts.limit);
    return this.request("GET", "/v1/api/admin/skills/discover", {
      params,
    });
  }

  async installSkill(body: SkillInstallRequest): Promise<SkillInstallResponse> {
    return this.request("POST", "/v1/api/admin/skills/install", {
      json: body,
    });
  }
}
