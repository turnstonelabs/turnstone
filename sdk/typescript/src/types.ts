// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export interface ErrorResponse {
  error: string;
}

export interface StatusResponse {
  status: string;
}

export interface DeleteSettingResponse {
  status: string;
  key: string;
  default: unknown;
}

export interface AuthLoginRequest {
  token: string;
}

export interface AuthLoginResponse {
  status: string;
  role: string;
  scopes?: string;
  jwt?: string;
  user_id?: string;
}

export interface AuthStatusResponse {
  auth_enabled: boolean;
  has_users: boolean;
  setup_required: boolean;
}

export interface AuthSetupResponse {
  status: string;
  user_id: string;
  username: string;
  role: string;
  scopes: string;
  jwt?: string;
}

// ---------------------------------------------------------------------------
// Server API — Workstream management
// ---------------------------------------------------------------------------

export interface SendRequest {
  message: string;
  ws_id: string;
}

export interface SendResponse {
  status: string;
}

export interface ApproveRequest {
  approved: boolean;
  feedback?: string | null;
  always?: boolean;
  ws_id: string;
}

export interface PlanFeedbackRequest {
  feedback: string;
  ws_id: string;
}

export interface CommandRequest {
  command: string;
  ws_id: string;
}

export interface CreateWorkstreamRequest {
  name?: string;
  model?: string;
  auto_approve?: boolean;
  resume_ws?: string;
  skill?: string;
}

export interface CreateWorkstreamResponse {
  ws_id: string;
  name: string;
  resumed?: boolean;
  message_count?: number;
}

export interface CloseWorkstreamRequest {
  ws_id: string;
}

export interface WorkstreamInfo {
  id: string;
  name: string;
  state: string;
}

export interface ListWorkstreamsResponse {
  workstreams: WorkstreamInfo[];
}

export interface DashboardWorkstream {
  id: string;
  name: string;
  state: string;
  title?: string;
  tokens?: number;
  context_ratio?: number;
  activity?: string;
  activity_state?: string;
  tool_calls?: number;
  node?: string;
  model?: string;
  model_alias?: string;
}

export interface DashboardAggregate {
  total_tokens: number;
  total_tool_calls: number;
  active_count: number;
  total_count: number;
  uptime_seconds?: number;
  node?: string;
}

export interface DashboardResponse {
  workstreams: DashboardWorkstream[];
  aggregate: DashboardAggregate;
}

// ---------------------------------------------------------------------------
// Server API — Saved workstreams
// ---------------------------------------------------------------------------

export interface SavedWorkstreamInfo {
  ws_id: string;
  alias?: string | null;
  title?: string | null;
  created: string;
  updated: string;
  message_count: number;
}

export interface ListSavedWorkstreamsResponse {
  workstreams: SavedWorkstreamInfo[];
}

// ---------------------------------------------------------------------------
// Server API — Skills
// ---------------------------------------------------------------------------

export interface SkillSummary {
  name: string;
  category: string;
  description: string;
  tags: string[];
  is_default: boolean;
  activation: string;
  origin: string;
  author: string;
  version: string;
}

export interface SkillInfo {
  template_id: string;
  name: string;
  category: string;
  content: string;
  description: string;
  tags: string[];
  variables: string;
  is_default: boolean;
  activation: string;
  org_id: string;
  created_by: string;
  origin: string;
  mcp_server: string;
  readonly: boolean;
  source_url: string;
  version: string;
  author: string;
  token_estimate: number;
  model: string;
  auto_approve: boolean;
  temperature: number | null;
  reasoning_effort: string;
  max_tokens: number | null;
  token_budget: number;
  agent_max_turns: number | null;
  notify_on_complete: string;
  enabled: boolean;
  priority: number;
  allowed_tools: string;
  license: string;
  compatibility: string;
  resource_count: number;
  created: string;
  updated: string;
}

export interface CreateSkillRequest {
  name: string;
  content: string;
  category?: string;
  description?: string;
  tags?: string;
  variables?: string;
  is_default?: boolean;
  activation?: string;
  org_id?: string;
  author?: string;
  version?: string;
  model?: string;
  auto_approve?: boolean;
  temperature?: number | null;
  reasoning_effort?: string;
  max_tokens?: number | null;
  token_budget?: number;
  agent_max_turns?: number | null;
  notify_on_complete?: string;
  enabled?: boolean;
  priority?: number;
  allowed_tools?: string;
  license?: string;
  compatibility?: string;
}

export interface UpdateSkillRequest {
  name?: string;
  content?: string;
  category?: string;
  description?: string;
  tags?: string;
  variables?: string;
  is_default?: boolean;
  activation?: string;
  author?: string;
  version?: string;
  model?: string;
  auto_approve?: boolean;
  temperature?: number | null;
  reasoning_effort?: string;
  max_tokens?: number | null;
  token_budget?: number;
  agent_max_turns?: number | null;
  notify_on_complete?: string;
  enabled?: boolean;
  priority?: number;
  allowed_tools?: string;
  license?: string;
  compatibility?: string;
}

export interface ListSkillsResponse {
  skills: SkillInfo[];
}

export interface SkillResourceInfo {
  resource_id: string;
  skill_id: string;
  path: string;
  content?: string;
  content_type: string;
  size: number;
  created: string;
}

export interface ListSkillResourcesResponse {
  resources: SkillResourceInfo[];
}

export interface CreateSkillResourceRequest {
  path: string;
  content: string;
  content_type?: string;
}

// ---------------------------------------------------------------------------
// Server API — Health
// ---------------------------------------------------------------------------

export interface BackendStatus {
  status: string;
}

export interface WorkstreamCounts {
  total: number;
  idle?: number;
  thinking?: number;
  running?: number;
  attention?: number;
  error?: number;
}

export interface McpStatus {
  servers: number;
  resources: number;
  prompts: number;
}

export interface HealthResponse {
  status: string;
  version?: string;
  uptime_seconds?: number;
  model?: string;
  workstreams?: WorkstreamCounts;
  backend?: BackendStatus | null;
  mcp?: McpStatus | null;
}

// ---------------------------------------------------------------------------
// Console API
// ---------------------------------------------------------------------------

export interface StateCounts {
  running?: number;
  thinking?: number;
  attention?: number;
  idle?: number;
  error?: number;
}

export interface ClusterAggregate {
  total_tokens: number;
  total_tool_calls: number;
}

export interface ClusterOverviewResponse {
  nodes: number;
  workstreams: number;
  states: StateCounts;
  aggregate: ClusterAggregate;
  version_drift: boolean;
  versions: string[];
}

export interface ClusterNodeInfo {
  node_id: string;
  server_url: string;
  ws_total: number;
  ws_running: number;
  ws_thinking: number;
  ws_attention: number;
  ws_idle: number;
  ws_error: number;
  total_tokens: number;
  started: number;
  reachable: boolean;
  health: Record<string, string>;
  version: string;
}

export interface ClusterNodesResponse {
  nodes: ClusterNodeInfo[];
  total: number;
}

export interface ClusterWorkstreamInfo {
  id: string;
  name: string;
  state: string;
  node: string;
  title?: string;
  tokens?: number;
  context_ratio?: number;
  activity?: string;
  activity_state?: string;
  tool_calls?: number;
}

export interface ClusterWorkstreamsResponse {
  workstreams: ClusterWorkstreamInfo[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export interface NodeDetailResponse {
  node_id: string;
  server_url: string;
  health: Record<string, string>;
  workstreams: ClusterWorkstreamInfo[];
  aggregate: ClusterAggregate;
}

export interface ClusterSnapshotNode {
  node_id: string;
  server_url: string;
  max_ws: number;
  reachable: boolean;
  version: string;
  health: Record<string, string>;
  aggregate: Record<string, number>;
  workstreams: ClusterWorkstreamInfo[];
}

export interface ClusterSnapshotResponse {
  nodes: ClusterSnapshotNode[];
  overview: ClusterOverviewResponse;
  timestamp: number;
}

export interface ConsoleCreateWsRequest {
  node_id?: string;
  name?: string;
  model?: string;
  initial_message?: string;
  skill?: string;
  resume_ws?: string;
}

export interface ConsoleCreateWsResponse {
  status: string;
  correlation_id: string;
  target_node: string;
}

export interface ConsoleHealthResponse {
  status: string;
  service: string;
  nodes: number;
  workstreams: number;
  version_drift: boolean;
  versions: string[];
}

// ---------------------------------------------------------------------------
// Console API — Schedules
// ---------------------------------------------------------------------------

export interface CreateScheduleRequest {
  name: string;
  schedule_type: string;
  initial_message: string;
  description?: string;
  cron_expr?: string;
  at_time?: string;
  target_mode?: string;
  model?: string;
  auto_approve?: boolean;
  auto_approve_tools?: string[];
  enabled?: boolean;
}

export interface UpdateScheduleRequest {
  name?: string;
  description?: string;
  schedule_type?: string;
  cron_expr?: string;
  at_time?: string;
  target_mode?: string;
  model?: string;
  initial_message?: string;
  auto_approve?: boolean;
  auto_approve_tools?: string[];
  enabled?: boolean;
}

export interface ScheduleInfo {
  task_id: string;
  name: string;
  description: string;
  schedule_type: string;
  cron_expr: string;
  at_time: string;
  target_mode: string;
  model: string;
  initial_message: string;
  auto_approve: boolean;
  auto_approve_tools: string[];
  enabled: boolean;
  created_by: string;
  last_run: string | null;
  next_run: string | null;
  created: string;
  updated: string;
}

export interface ListSchedulesResponse {
  schedules: ScheduleInfo[];
}

export interface ScheduleRunInfo {
  run_id: string;
  task_id: string;
  node_id: string;
  ws_id: string;
  correlation_id: string;
  started: string;
  status: string;
  error: string;
}

export interface ListScheduleRunsResponse {
  runs: ScheduleRunInfo[];
}

// ---------------------------------------------------------------------------
// Console API — Governance: Roles
// ---------------------------------------------------------------------------

export interface RoleInfo {
  role_id: string;
  name: string;
  display_name: string;
  permissions: string;
  builtin: boolean;
  org_id: string;
  created: string;
  updated: string;
}

export interface CreateRoleOptions {
  name: string;
  display_name?: string;
  permissions?: string;
}

export interface UpdateRoleOptions {
  display_name?: string;
  permissions?: string;
}

export interface UserRoleInfo extends RoleInfo {
  assigned_by: string;
  assignment_created: string;
}

// ---------------------------------------------------------------------------
// Console API — Governance: Orgs
// ---------------------------------------------------------------------------

export interface OrgInfo {
  org_id: string;
  name: string;
  display_name: string;
  settings: string;
  created: string;
  updated: string;
}

export interface UpdateOrgOptions {
  display_name?: string;
  settings?: string;
}

// ---------------------------------------------------------------------------
// Console API — Governance: Tool Policies
// ---------------------------------------------------------------------------

export interface ToolPolicyInfo {
  policy_id: string;
  name: string;
  tool_pattern: string;
  action: string;
  priority: number;
  org_id: string;
  enabled: boolean;
  created_by: string;
  created: string;
  updated: string;
}

export interface CreatePolicyOptions {
  name: string;
  tool_pattern: string;
  action: string;
  priority?: number;
  org_id?: string;
  enabled?: boolean;
}

export interface UpdatePolicyOptions {
  name?: string;
  tool_pattern?: string;
  action?: string;
  priority?: number;
  enabled?: boolean;
}

// ---------------------------------------------------------------------------
// Console API — Governance: Usage & Audit
// ---------------------------------------------------------------------------

export interface UsageBreakdownItem {
  key?: string;
  prompt_tokens: number;
  completion_tokens: number;
  tool_calls_count: number;
}

export interface UsageResponse {
  summary: UsageBreakdownItem[];
  breakdown: UsageBreakdownItem[];
}

export interface UsageQueryOptions {
  since: string;
  until?: string;
  user_id?: string;
  model?: string;
  group_by?: string;
}

export interface AuditEventInfo {
  event_id: string;
  timestamp: string;
  user_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail: string;
  ip_address: string;
  created: string;
}

export interface AuditQueryOptions {
  action?: string;
  user_id?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export interface AuditResponse {
  events: AuditEventInfo[];
  total: number;
}

// ---------------------------------------------------------------------------
// SDK-specific types
// ---------------------------------------------------------------------------

export interface TurnResult {
  wsId: string;
  contentParts: string[];
  reasoningParts: string[];
  toolResults: Array<{ name: string; output: string }>;
  errors: string[];
  timedOut: boolean;
  content: string;
  reasoning: string;
  ok: boolean;
}

export interface SendAndWaitOptions {
  /** Timeout in milliseconds (default: 600000 = 10 minutes). */
  timeout?: number;
  onEvent?: (event: import("./events.js").ServerEvent) => void;
}

export interface NodesOptions {
  sort?: string;
  limit?: number;
  offset?: number;
}

export interface WorkstreamsOptions {
  state?: string;
  node?: string;
  search?: string;
  sort?: string;
  page?: number;
  per_page?: number;
}

// -- Server API: Memories ---------------------------------------------------

export interface SaveMemoryRequest {
  name: string;
  content: string;
  description?: string;
  type?: "user" | "project" | "feedback" | "reference";
  scope?: "global" | "workstream" | "user";
  scope_id?: string;
}

export interface MemoryInfo {
  memory_id: string;
  name: string;
  description: string;
  type: string;
  scope: string;
  scope_id: string;
  content: string;
  created: string;
  updated: string;
}

export interface ListMemoriesResponse {
  memories: MemoryInfo[];
  total: number;
}

export interface SearchMemoriesRequest {
  query: string;
  type?: string;
  scope?: string;
  scope_id?: string;
  limit?: number;
}

export interface ListMemoriesOptions {
  type?: string;
  scope?: string;
  scope_id?: string;
  limit?: number;
}

export interface DeleteMemoryOptions {
  scope?: string;
  scope_id?: string;
}

// -- Console API: Admin Memories --------------------------------------------

export interface AdminMemoryInfo {
  memory_id: string;
  name: string;
  description: string;
  type: string;
  scope: string;
  scope_id: string;
  content: string;
  created: string;
  updated: string;
  last_accessed: string;
  access_count: number;
}

export interface ListAdminMemoriesResponse {
  memories: AdminMemoryInfo[];
  total: number;
}

export interface AdminListMemoriesOptions {
  type?: string;
  scope?: string;
  scope_id?: string;
  limit?: number;
}

export interface AdminSearchMemoriesOptions {
  q: string;
  type?: string;
  scope?: string;
  scope_id?: string;
  limit?: number;
}

// -- Console API: MCP Servers -----------------------------------------------

export interface McpServerStatus {
  connected: boolean;
  tools: number;
  resources: number;
  prompts: number;
  error: string;
}

export interface McpServerDetail {
  server_id: string;
  name: string;
  transport: string;
  command: string;
  args: string;
  url: string;
  headers: string;
  env: string;
  auto_approve: boolean;
  enabled: boolean;
  created_by: string;
  registry_name: string | null;
  registry_version: string;
  registry_meta: string;
  created: string;
  updated: string;
  status: Record<string, McpServerStatus>;
}

export interface ListMcpServersResponse {
  servers: McpServerDetail[];
}

export interface CreateMcpServerRequest {
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
  env?: Record<string, string>;
  auto_approve?: boolean;
  enabled?: boolean;
}

export interface UpdateMcpServerRequest {
  name?: string;
  transport?: string;
  command?: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
  env?: Record<string, string>;
  auto_approve?: boolean;
  enabled?: boolean;
}

export interface ImportMcpConfigResponse {
  imported: string[];
  skipped: string[];
  errors: string[];
}

// -- Console API: MCP Registry ----------------------------------------------

export interface RegistryRemoteInfo {
  type: string;
  url: string;
  headers: Record<string, unknown>[];
  variables: Record<string, Record<string, unknown>>;
}

export interface RegistryPackageInfo {
  registry_type: string;
  identifier: string;
  version: string;
  transport_type: string;
  environment_variables: Record<string, unknown>[];
}

export interface RegistryServerInfo {
  name: string;
  description: string;
  title: string;
  version: string;
  website_url: string;
  repository: Record<string, string>;
  icons: Record<string, string>[];
  remotes: RegistryRemoteInfo[];
  packages: RegistryPackageInfo[];
  meta: Record<string, unknown>;
  installed: boolean;
  installed_server_id: string;
  installed_version: string;
  update_available: boolean;
}

export interface RegistrySearchResponse {
  servers: RegistryServerInfo[];
  total: number;
  next_cursor: string | null;
}

export interface RegistryInstallRequest {
  registry_name: string;
  source: string;
  index?: number;
  name?: string;
  variables?: Record<string, string>;
  env?: Record<string, string>;
  headers?: Record<string, string>;
}

// -- Console API: Skill Discovery -------------------------------------------

export interface SkillDiscoverListing {
  id: string;
  name: string;
  description: string;
  author: string;
  source: string;
  source_url: string;
  install_count: number;
  tags: string[];
  installed: boolean;
  scan_status?: string;
  template_id?: string;
}

export interface SkillDiscoverResponse {
  skills: SkillDiscoverListing[];
}

export interface SkillInstallRequest {
  source: string;
  skill_id?: string;
  url?: string;
}

export interface SkillInstallSkipped {
  name: string;
  reason: string;
}

export interface SkillInstallResponse {
  installed: SkillInfo[];
  skipped: SkillInstallSkipped[];
  total: number;
}

// -- Console API: System Settings -------------------------------------------

export interface SettingInfo {
  key: string;
  value: unknown;
  source: string;
  type: string;
  description: string;
  section: string;
  is_secret: boolean;
  node_id: string;
  changed_by: string;
  updated: string;
  restart_required: boolean;
}

export interface ListSettingsResponse {
  settings: SettingInfo[];
}

export interface SettingSchemaInfo {
  key: string;
  type: string;
  default: unknown;
  description: string;
  section: string;
  is_secret: boolean;
  min_value: number | null;
  max_value: number | null;
  choices: string[] | null;
  restart_required: boolean;
}

export interface ListSettingSchemaResponse {
  schema: SettingSchemaInfo[];
}

export interface UpdateSettingOptions {
  value: unknown;
  node_id?: string;
}

// Re-export event types for convenience
export type { ServerEvent, ClusterEvent } from "./events.js";
