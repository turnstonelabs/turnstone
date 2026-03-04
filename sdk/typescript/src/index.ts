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
  SessionInfo,
  ListSessionsResponse,
  BackendStatus,
  WorkstreamCounts,
  HealthResponse,
  AuthLoginRequest,
  AuthLoginResponse,
  StatusResponse,
  ErrorResponse,
  ClusterOverviewResponse,
  ClusterNodeInfo,
  ClusterNodesResponse,
  ClusterWorkstreamInfo,
  ClusterWorkstreamsResponse,
  NodeDetailResponse,
  ConsoleCreateWsRequest,
  ConsoleCreateWsResponse,
  ConsoleHealthResponse,
  TurnResult,
  SendAndWaitOptions,
  NodesOptions,
  WorkstreamsOptions,
} from "./types.js";

// SSE parser (for advanced usage)
export { parseSSEStream } from "./sse.js";
