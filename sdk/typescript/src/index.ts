/**
 * @turnstone/sdk — TypeScript client SDK for the turnstone AI orchestration platform.
 *
 * @example
 * ```ts
 * import { TurnstoneServer } from "@turnstone/sdk";
 *
 * const client = new TurnstoneServer({
 *   baseUrl: "http://localhost:8080",
 *   token: "ts_your_api_token",
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
export type { ClientOptions, TlsOptions } from "./base.js";

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
  StateChangeEvent,
  ToolInfoEvent,
  ApproveRequestEvent,
  ApprovalResolvedEvent,
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
  isStateChangeEvent,
  isToolResultEvent,
  isWsStateEvent,
  isApproveRequestEvent,
  isApprovalResolvedEvent,
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
  McpStatus,
  WorkstreamCounts,
  HealthResponse,
  AuthLoginRequest,
  AuthLoginResponse,
  AuthStatusResponse,
  AuthSetupResponse,
  DeleteSettingResponse,
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
  SkillSummary,
  SkillInfo,
  CreateSkillRequest,
  UpdateSkillRequest,
  ListSkillsResponse,
  SkillResourceInfo,
  ListSkillResourcesResponse,
  CreateSkillResourceRequest,
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
  // Memory types
  SaveMemoryRequest,
  MemoryInfo,
  ListMemoriesResponse,
  SearchMemoriesRequest,
  ListMemoriesOptions,
  DeleteMemoryOptions,
  AdminMemoryInfo,
  ListAdminMemoriesResponse,
  AdminListMemoriesOptions,
  AdminSearchMemoriesOptions,
  // Settings types
  SettingInfo,
  ListSettingsResponse,
  SettingSchemaInfo,
  ListSettingSchemaResponse,
  UpdateSettingOptions,
  // MCP server types
  McpServerStatus,
  McpServerDetail,
  ListMcpServersResponse,
  CreateMcpServerRequest,
  UpdateMcpServerRequest,
  ImportMcpConfigResponse,
  // MCP registry types
  RegistryRemoteInfo,
  RegistryPackageInfo,
  RegistryServerInfo,
  RegistrySearchResponse,
  RegistryInstallRequest,
  // Skill discovery types
  SkillDiscoverListing,
  SkillDiscoverResponse,
  SkillInstallRequest,
  SkillInstallResponse,
  SkillInstallSkipped,
  // Attachment types
  AttachmentUpload,
  AttachmentInfo,
  UploadAttachmentResponse,
  ListAttachmentsResponse,
  AttachmentContent,
} from "./types.js";

// SSE parser (for advanced usage)
export { parseSSEStream } from "./sse.js";
