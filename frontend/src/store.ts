import type { Agent, BandStatus, Bootstrap, Channel, Message, Model, Role, ServerEvent, User } from "./types";

export interface State {
  me: User;
  users: Record<number, User>;
  channels: Record<number, Channel>;
  agents: Record<number, Agent>;
  messages: Record<number, Message[]>;
  typing: Record<number, number[]>;
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
  | { type: "select_channel"; channelId: number };

const byId = <T extends { id: number }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function initialState(boot: Bootstrap): State {
  const general = boot.channels.find((c) => c.name === "general") ?? boot.channels[0];
  return {
    me: boot.me,
    users: byId(boot.users),
    channels: byId(boot.channels),
    agents: byId(boot.agents),
    messages: {},
    typing: {},
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

function applyEvent(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "message": {
      const { message } = event;
      const loaded = state.messages[message.channel_id];
      const isActive = message.channel_id === state.activeChannelId;
      return {
        ...state,
        messages: loaded ? { ...state.messages, [message.channel_id]: mergeMessages(loaded, [message]) } : state.messages,
        unread: isActive
          ? state.unread
          : { ...state.unread, [message.channel_id]: (state.unread[message.channel_id] ?? 0) + 1 },
      };
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
      return { ...state, agents };
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
    case "select_channel":
      return { ...state, activeChannelId: action.channelId, unread: { ...state.unread, [action.channelId]: 0 } };
  }
}
