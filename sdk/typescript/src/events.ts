import type { ClusterOverviewResponse, ClusterSnapshotNode } from "./types.js";

// ---------------------------------------------------------------------------
// Server SSE events
// ---------------------------------------------------------------------------

export interface ConnectedEvent {
  type: "connected";
  model: string;
  model_alias: string;
  skip_permissions: boolean;
}

export interface HistoryEvent {
  type: "history";
  messages: Array<Record<string, unknown>>;
}

export interface ThinkingStartEvent {
  type: "thinking_start";
}

export interface ThinkingStopEvent {
  type: "thinking_stop";
}

export interface ContentEvent {
  type: "content";
  text: string;
}

export interface ReasoningEvent {
  type: "reasoning";
  text: string;
}

export interface StreamEndEvent {
  type: "stream_end";
}

export interface ToolInfoEvent {
  type: "tool_info";
  items: Array<Record<string, unknown>>;
}

export interface ApproveRequestEvent {
  type: "approve_request";
  items: Array<Record<string, unknown>>;
}

export interface ToolResultEvent {
  type: "tool_result";
  call_id: string;
  name: string;
  output: string;
}

export interface ToolOutputChunkEvent {
  type: "tool_output_chunk";
  call_id: string;
  chunk: string;
}

export interface StatusEvent {
  type: "status";
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  context_window: number;
  pct: number;
  effort: string;
}

export interface PlanReviewEvent {
  type: "plan_review";
  content: string;
}

export interface InfoEvent {
  type: "info";
  message: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface BusyErrorEvent {
  type: "busy_error";
  message: string;
}

export interface ClearUiEvent {
  type: "clear_ui";
}

export interface CancelledEvent {
  type: "cancelled";
}

// Global events

export interface WsStateEvent {
  type: "ws_state";
  ws_id: string;
  state: string;
  tokens: number;
  context_ratio: number;
  activity: string;
  activity_state: string;
}

export interface WsActivityEvent {
  type: "ws_activity";
  ws_id: string;
  activity: string;
  activity_state: string;
}

export interface WsRenameEvent {
  type: "ws_rename";
  ws_id: string;
  name: string;
}

export interface WsClosedEvent {
  type: "ws_closed";
  ws_id: string;
  name?: string;
}

/** Discriminated union of all server SSE event types. */
export type ServerEvent =
  | ConnectedEvent
  | HistoryEvent
  | ThinkingStartEvent
  | ThinkingStopEvent
  | ContentEvent
  | ReasoningEvent
  | StreamEndEvent
  | ToolInfoEvent
  | ApproveRequestEvent
  | ToolResultEvent
  | ToolOutputChunkEvent
  | StatusEvent
  | PlanReviewEvent
  | InfoEvent
  | ErrorEvent
  | BusyErrorEvent
  | ClearUiEvent
  | CancelledEvent
  | WsStateEvent
  | WsActivityEvent
  | WsRenameEvent
  | WsClosedEvent;

// ---------------------------------------------------------------------------
// Console cluster SSE events
// ---------------------------------------------------------------------------

export interface NodeJoinedEvent {
  type: "node_joined";
  node_id: string;
}

export interface NodeLostEvent {
  type: "node_lost";
  node_id: string;
}

export interface ClusterStateEvent {
  type: "cluster_state";
  ws_id: string;
  node_id: string;
  state: string;
  tokens: number;
  context_ratio: number;
  activity: string;
  activity_state: string;
}

export interface ClusterWsCreatedEvent {
  type: "ws_created";
  ws_id: string;
  node_id: string;
  name: string;
}

export interface ClusterWsClosedEvent {
  type: "ws_closed";
  ws_id: string;
}

export interface ClusterWsRenameEvent {
  type: "ws_rename";
  ws_id: string;
  name: string;
}

export interface ClusterSnapshotEvent {
  type: "snapshot";
  nodes: ClusterSnapshotNode[];
  overview: ClusterOverviewResponse;
  timestamp: number;
}

/** Discriminated union of all console cluster SSE event types. */
export type ClusterEvent =
  | NodeJoinedEvent
  | NodeLostEvent
  | ClusterStateEvent
  | ClusterWsCreatedEvent
  | ClusterWsClosedEvent
  | ClusterWsRenameEvent
  | ClusterSnapshotEvent;

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

export function isContentEvent(e: ServerEvent): e is ContentEvent {
  return e.type === "content";
}

export function isReasoningEvent(e: ServerEvent): e is ReasoningEvent {
  return e.type === "reasoning";
}

export function isErrorEvent(e: ServerEvent): e is ErrorEvent {
  return e.type === "error";
}

export function isStreamEndEvent(e: ServerEvent): e is StreamEndEvent {
  return e.type === "stream_end";
}

export function isToolResultEvent(e: ServerEvent): e is ToolResultEvent {
  return e.type === "tool_result";
}

export function isWsStateEvent(e: ServerEvent): e is WsStateEvent {
  return e.type === "ws_state";
}

export function isApproveRequestEvent(
  e: ServerEvent,
): e is ApproveRequestEvent {
  return e.type === "approve_request";
}

export function isPlanReviewEvent(e: ServerEvent): e is PlanReviewEvent {
  return e.type === "plan_review";
}

export function isCancelledEvent(e: ServerEvent): e is CancelledEvent {
  return e.type === "cancelled";
}
