import type { Agent, BandStatus, Bootstrap, Channel, Draft, Message, Model, Role, ServerEvent, TaskActivity, User } from "./types";

export interface State {
  me: User;
  users: Record<number, User>;
  channels: Record<number, Channel>;
  agents: Record<number, Agent>;
  messages: Record<number, Message[]>;
  typing: Record<number, number[]>;
  activity: Record<number, Record<string, TaskActivity>>;
  drafts: Record<number, Record<string, Draft>>;
  unread: Record<number, number>;
  roles: Role[];
  models: Model[];
  band: BandStatus;
  anthropicConfigured: boolean;
  activeChannelId: number | null;
}

export type Action =
  | { type: "event"; event: ServerEvent }
  | { type: "messages_loaded"; channelId: number; messages: Message[] }
  | { type: "tasks_loaded"; channelId: number; tasks: TaskActivity[] }
  | { type: "select_channel"; channelId: number };

const byId = <T extends { id: number }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function initialState(boot: Bootstrap): State {
  const general = boot.channels.find((c) => c.name === "general") ?? boot.channels.find((c) => !c.is_dm);
  return {
    me: boot.me,
    users: byId(boot.users),
    channels: byId(boot.channels),
    agents: byId(boot.agents),
    messages: {},
    typing: {},
    activity: {},
    drafts: {},
    unread: {},
    roles: boot.roles,
    models: boot.models,
    band: boot.band,
    anthropicConfigured: boot.anthropic_configured,
    activeChannelId: general?.id ?? null,
  };
}

function mergeMessages(existing: Message[] | undefined, incoming: Message[]): Message[] {
  const map = new Map((existing ?? []).map((m) => [m.id, m]));
  for (const m of incoming) map.set(m.id, m);
  return [...map.values()].sort((a, b) => a.id - b.id);
}

function withoutDrafts(state: State, channelId: number, drop: (d: Draft) => boolean): State {
  const current = state.drafts[channelId];
  if (!current || !Object.values(current).some(drop)) return state;
  const kept = Object.fromEntries(Object.entries(current).filter(([, d]) => !drop(d)));
  return { ...state, drafts: { ...state.drafts, [channelId]: kept } };
}

function applyEvent(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "message": {
      const { message } = event;
      const loaded = state.messages[message.channel_id];
      const isActive = message.channel_id === state.activeChannelId;
      // An agent runs one task per channel at a time, so its message replaces its draft there.
      const rest =
        message.author_type === "agent"
          ? withoutDrafts(state, message.channel_id, (d) => d.agent_id === message.author_id)
          : state;
      return {
        ...rest,
        messages: loaded ? { ...state.messages, [message.channel_id]: mergeMessages(loaded, [message]) } : state.messages,
        unread: isActive
          ? state.unread
          : { ...state.unread, [message.channel_id]: (state.unread[message.channel_id] ?? 0) + 1 },
      };
    }
    case "message_updated": {
      const loaded = state.messages[event.message.channel_id];
      if (!loaded?.some((m) => m.id === event.message.id)) return state;
      return { ...state, messages: { ...state.messages, [event.message.channel_id]: mergeMessages(loaded, [event.message]) } };
    }
    case "channel":
      return { ...state, channels: { ...state.channels, [event.channel.id]: event.channel } };
    case "user":
      return { ...state, users: { ...state.users, [event.user.id]: event.user } };
    case "presence": {
      const user = state.users[event.user_id];
      if (!user) return state;
      return { ...state, users: { ...state.users, [user.id]: { ...user, online: event.online } } };
    }
    case "agent":
      return { ...state, agents: { ...state.agents, [event.agent.id]: event.agent } };
    case "agent_deleted": {
      const agents = { ...state.agents };
      delete agents[event.agent_id];
      const channels = Object.fromEntries(
        Object.values(state.channels)
          .filter((c) => !c.is_dm || (c.dm_agent_id !== null && c.dm_agent_id !== event.agent_id))
          .map((c) => [c.id, c]),
      );
      const activeChannelId =
        state.activeChannelId != null && !channels[state.activeChannelId]
          ? (Object.values(channels).find((c) => c.name === "general")?.id ?? null)
          : state.activeChannelId;
      return { ...state, agents, channels, activeChannelId };
    }
    case "agent_status": {
      const agent = state.agents[event.agent_id];
      if (!agent) return state;
      return { ...state, agents: { ...state.agents, [agent.id]: { ...agent, status: event.status } } };
    }
    case "typing": {
      const current = (state.typing[event.channel_id] ?? []).filter((id) => id !== event.agent_id);
      return {
        ...state,
        typing: { ...state.typing, [event.channel_id]: event.active ? [...current, event.agent_id] : current },
      };
    }
    case "band_status":
      return { ...state, band: event.band };
    case "task_activity": {
      const { task } = event;
      const current = { ...state.activity[task.channel_id] };
      if (task.status === "done") delete current[task.id];
      else current[task.id] = task;
      const next = { ...state, activity: { ...state.activity, [task.channel_id]: current } };
      return task.status === "done" ? withoutDrafts(next, task.channel_id, (d) => d.task_id === task.id) : next;
    }
    case "draft": {
      const { type: _type, ...draft } = event;
      if (!draft.content) return withoutDrafts(state, draft.channel_id, (d) => d.task_id === draft.task_id);
      return {
        ...state,
        drafts: { ...state.drafts, [draft.channel_id]: { ...state.drafts[draft.channel_id], [draft.task_id]: draft } },
      };
    }
  }
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "event":
      return applyEvent(state, action.event);
    case "messages_loaded":
      return {
        ...state,
        messages: { ...state.messages, [action.channelId]: mergeMessages(state.messages[action.channelId], action.messages) },
      };
    case "tasks_loaded": {
      // Polling seeds activity for clients that connected mid-task; WebSocket events keep it live.
      const active = action.tasks.filter((t) => t.status !== "done");
      return { ...state, activity: { ...state.activity, [action.channelId]: Object.fromEntries(active.map((t) => [t.id, t])) } };
    }
    case "select_channel":
      return { ...state, activeChannelId: action.channelId, unread: { ...state.unread, [action.channelId]: 0 } };
  }
}
