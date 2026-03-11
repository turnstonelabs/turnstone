/**
 * @turnstone/sdk — TypeScript client SDK for the turnstone AI orchestration platform.
 *
 * @example
 * ```ts
 * import { TurnstoneServer } from "@turnstone/sdk";
 *
 * const client = new TurnstoneServer({
 *   baseUrl: "http://localhost:8080",
 *   token: "tok_xxx",
 * });
 *
 * const ws = await client.createWorkstream({ name: "demo" });
 * const result = await client.sendAndWait("Hello!", ws.ws_id);
 * console.log(result.content);
 * ```
 */

// Clients
export { TurnstoneServer } from "./server.js";
export { TurnstoneConsole } from "./console.js";
export type { ClientOptions } from "./base.js";

// Errors
export { TurnstoneAPIError } from "./errors.js";

// Event types and guards
export type {
  ServerEvent,
  ClusterEvent,
  ConnectedEvent,
  HistoryEvent,
  ThinkingStartEvent,
  ThinkingStopEvent,
  ContentEvent,
  ReasoningEvent,
  StreamEndEvent,
  ToolInfoEvent,
  ApproveRequestEvent,
  ToolResultEvent,
  ToolOutputChunkEvent,
  StatusEvent,
  PlanReviewEvent,
  InfoEvent,
  ErrorEvent,
  BusyErrorEvent,
  ClearUiEvent,
  CancelledEvent,
  WsStateEvent,
  WsActivityEvent,
  WsRenameEvent,
  WsClosedEvent,
  NodeJoinedEvent,
  NodeLostEvent,
  ClusterStateEvent,
  ClusterWsCreatedEvent,
  ClusterWsClosedEvent,
  ClusterWsRenameEvent,
  ClusterSnapshotEvent,
} from "./events.js";

export {
  isContentEvent,
  isReasoningEvent,
  isErrorEvent,
  isStreamEndEvent,
  isToolResultEvent,
  isWsStateEvent,
  isApproveRequestEvent,
  isPlanReviewEvent,
  isCancelledEvent,
} from "./events.js";

// Request/response types
export type {
  SendRequest,
  SendResponse,
  ApproveRequest,
  PlanFeedbackRequest,
  CommandRequest,
  CreateWorkstreamRequest,
  CreateWorkstreamResponse,
  CloseWorkstreamRequest,
  WorkstreamInfo,
  ListWorkstreamsResponse,
  DashboardWorkstream,
  DashboardAggregate,
  DashboardResponse,
  SavedWorkstreamInfo,
  ListSavedWorkstreamsResponse,
  BackendStatus,
  WorkstreamCounts,
  HealthResponse,
  AuthLoginRequest,
  AuthLoginResponse,
  AuthStatusResponse,
  AuthSetupResponse,
  StatusResponse,
  ErrorResponse,
  ClusterOverviewResponse,
  ClusterNodeInfo,
  ClusterNodesResponse,
  ClusterSnapshotNode,
  ClusterSnapshotResponse,
  ClusterWorkstreamInfo,
  ClusterWorkstreamsResponse,
  NodeDetailResponse,
  ConsoleCreateWsRequest,
  ConsoleCreateWsResponse,
  ConsoleHealthResponse,
  CreateScheduleRequest,
  UpdateScheduleRequest,
  ScheduleInfo,
  ScheduleRunInfo,
  ListSchedulesResponse,
  ListScheduleRunsResponse,
  RoleInfo,
  CreateRoleOptions,
  UpdateRoleOptions,
  UserRoleInfo,
  OrgInfo,
  UpdateOrgOptions,
  ToolPolicyInfo,
  CreatePolicyOptions,
  UpdatePolicyOptions,
  PromptTemplateInfo,
  CreateTemplateOptions,
  UpdateTemplateOptions,
  UsageBreakdownItem,
  UsageResponse,
  UsageQueryOptions,
  AuditEventInfo,
  AuditQueryOptions,
  AuditResponse,
  TurnResult,
  SendAndWaitOptions,
  NodesOptions,
  WorkstreamsOptions,
} from "./types.js";

// SSE parser (for advanced usage)
export { parseSSEStream } from "./sse.js";
