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
  is_dm: boolean;
  dm_agent_id: number | null;
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
  type: "agent" | "human";
  id: number;
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
  created_at: string;
}

export interface TaskActivity {
  id: string;
  channel_id: number;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "done";
  progress: string;
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
  models: Model[];
  band: BandStatus;
  anthropic_configured: boolean;
}

export type ServerEvent =
  | { type: "message"; message: Message }
  | { type: "channel"; channel: Channel }
  | { type: "user"; user: User }
  | { type: "presence"; user_id: number; online: boolean }
  | { type: "agent"; agent: Agent }
  | { type: "agent_deleted"; agent_id: number }
  | { type: "agent_status"; agent_id: number; status: AgentStatus }
  | { type: "typing"; channel_id: number; agent_id: number; active: boolean }
  | { type: "band_status"; band: BandStatus }
  | { type: "task_activity"; task: TaskActivity };
