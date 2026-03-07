// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export interface ErrorResponse {
  error: string;
}

export interface StatusResponse {
  status: string;
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
// Server API — Health
// ---------------------------------------------------------------------------

export interface BackendStatus {
  status: string;
  circuit_state: string;
}

export interface WorkstreamCounts {
  total: number;
  idle?: number;
  thinking?: number;
  running?: number;
  attention?: number;
  error?: number;
}

export interface HealthResponse {
  status: string;
  version?: string;
  uptime_seconds?: number;
  model?: string;
  workstreams?: WorkstreamCounts;
  backend?: BackendStatus | null;
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

export interface ConsoleCreateWsRequest {
  node_id?: string;
  name?: string;
  model?: string;
  initial_message?: string;
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

// Re-export event types for convenience
export type { ServerEvent, ClusterEvent } from "./events.js";
