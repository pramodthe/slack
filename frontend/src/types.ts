import type { ChartVariant } from "./components/assistant-ui/elements/chart";
import type { DataTableColumn, DataTableRow } from "./components/assistant-ui/elements/data-table";
import type { SpecRow } from "./components/assistant-ui/elements/spec-sheet";

export interface User {
  id: number;
  username: string;
  display_name: string;
  online: boolean;
}

export interface Channel {
  id: number;
  name: string;
  topic: string;
  band_room_id: string | null;
  human_ids: number[];
  agent_ids: number[];
  outside_agents: OutsideAgent[];
  is_dm: boolean;
  dm_agent_id: number | null;
  policy: ChannelPolicy;
}

export type WorkspaceRole = "owner" | "admin" | "member";

export interface ChannelPolicy {
  approvers: "members" | "admins";
  computer_control: "members" | "admins";
}

export interface OutsideAgent {
  band_id: string;
  handle: string;
  name: string;
  description: string;
}

export interface BandPeer extends OutsideAgent {
  online: boolean;
  is_external: boolean;
}

export interface ChannelComputer {
  agent_id: number;
  task_id: string;
  status: string;
  last_action: string;
  updated_at: string;
  agent_working: boolean;
}

export interface AgentStatus {
  state: "starting" | "online" | "stopped" | "error";
  error: string | null;
}

export interface Agent {
  id: number;
  name: string;
  handle: string;
  role: string;
  role_label: string;
  instructions: string;
  model: string;
  band_agent_id: string;
  is_master: boolean;
  status: AgentStatus;
  created_by: number | null;
  created_by_agent: number | null;
}

export interface Mention {
  type: "agent" | "human" | "external";
  id: number | null;
  name: string;
}

export interface Message {
  id: number;
  channel_id: number;
  author_type: "human" | "agent" | "system" | "external";
  author_id: number | null;
  author_name: string;
  content: string;
  mentions: Mention[];
  to_humans: boolean;
  components: MessageComponent[];
  approval: MessageApproval | null;
  handoff: boolean;
  created_at: string;
}

export interface MessageApproval {
  id: string;
  status: "pending" | "approved" | "denied";
  action: string;
  details: string;
  decided_by: string;
  note: string;
}

export type MessageComponent =
  | { id: number; kind: "table"; props: { caption: string; columns: DataTableColumn[]; rows: DataTableRow[] } }
  | {
      id: number;
      kind: "chart";
      props: {
        label: string;
        value: string;
        points: number[];
        visibleCount: number;
        delta?: string;
        trend?: "up" | "down" | "flat";
        upIsGood?: boolean;
        variant?: ChartVariant;
      };
    }
  | { id: number; kind: "spec"; props: { title: string; subtitle?: string; rows: SpecRow[]; visibleCount: number } };

export interface TaskActivity {
  id: string;
  channel_id: number;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "done";
  progress: string;
}

export interface Task {
  id: string;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled";
  progress: string;
  error: string | null;
  attempts: number;
  cancel_requested: boolean;
  delivery_status: string;
  created_at: string;
  updated_at: string;
  usage?: TaskUsage | null;
}

export interface TaskUsage {
  input_tokens: number;
  output_tokens: number;
  cost: number | null;
}

export interface AgentUsage {
  agent_id: number;
  tokens: number;
  cost: number | null;
  agent_tokens_today: number;
  agent_budget: number | null;
}

export interface ChannelUsage {
  since: string;
  tokens: number;
  cost: number | null;
  budget: number | null;
  agents: AgentUsage[];
  prices_configured: boolean;
}

export interface AuditEvent {
  id: number;
  at: string;
  channel_id: number | null;
  task_id: string | null;
  actor_type: "human" | "agent" | "system";
  actor_id: number | null;
  actor_name: string;
  action: string;
  detail: Record<string, unknown>;
}

export interface Artifact {
  path: string;
  content: string;
}

export interface TaskDetail extends Task {
  result: string;
  artifacts: Artifact[];
}

export interface Schedule {
  id: number;
  agent_id: number;
  prompt: string;
  when: string;
  next_run_at: string | null;
  last_run_at: string | null;
}

export interface Memory {
  key: string;
  content: string;
  updated_at: string;
}

export interface ComputerControl {
  user_name: string;
  until: string;
  mine: boolean;
}

export interface ComputerState {
  configured: boolean;
  status: string;
  sandbox_id: string | null;
  image: string;
  width: number;
  height: number;
  last_action: string;
  updated_at: string | null;
  error: string | null;
  dashboard_url: string;
  control: ComputerControl | null;
}

export interface LiveView {
  url: string;
  expires_at: string;
  control: ComputerControl | null;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

export interface Role {
  id: string;
  label: string;
  description: string;
  instructions: string;
}

export interface Model {
  id: string;
  label: string;
}

export interface BandStatus {
  configured: boolean;
  online: boolean;
  owner_name: string | null;
  owner_handle: string | null;
  error: string | null;
}

export interface Bootstrap {
  me: User;
  users: User[];
  channels: Channel[];
  agents: Agent[];
  roles: Role[];
  workspace_roles: Record<string, WorkspaceRole>;
  models: Model[];
  band: BandStatus;
  anthropic_configured: boolean;
}

export interface Draft {
  task_id: string;
  channel_id: number;
  agent_id: number;
  content: string;
}

export type ServerEvent =
  | { type: "message"; message: Message }
  | { type: "message_updated"; message: Message }
  | ({ type: "draft" } & Draft)
  | { type: "draft_append"; task_id: string; channel_id: number; agent_id: number; offset: number; text: string }
  | { type: "channel"; channel: Channel }
  | { type: "user"; user: User }
  | { type: "presence"; user_id: number; online: boolean }
  | { type: "agent"; agent: Agent }
  | { type: "agent_deleted"; agent_id: number }
  | { type: "agent_status"; agent_id: number; status: AgentStatus }
  | { type: "typing"; channel_id: number; agent_id: number; active: boolean }
  | { type: "band_status"; band: BandStatus }
  | { type: "task_activity"; task: TaskActivity }
  | { type: "workspace_role"; user_id: number; role: WorkspaceRole }
  | { type: "budget"; scope: "agent" | "channel"; scope_id: number; daily_tokens: number | null };
