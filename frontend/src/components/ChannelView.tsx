import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type Dispatch } from "react";
import type { State, Action } from "../store";
import type { Channel, ChannelComputer, Draft, Message } from "../types";
import Avatar from "./Avatar";
import Composer from "./Composer";
import TaskPanel from "./TaskPanel";
import RichText, { MentionNames } from "./RichText";
import MessageComponents from "./MessageComponents";
import ApprovalRequest from "./ApprovalRequest";
import { AgentHandoff } from "./assistant-ui/elements/agent-handoff";
import { AgentStatus } from "./assistant-ui/elements/agent-status";
import type { Modal, OpenComputer } from "./Workspace";

const GROUP_WINDOW_MS = 5 * 60 * 1000;

function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatDay(iso: string) {
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return "Today";
  return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

const isChatter = (m: Message) => m.author_type === "agent" && !m.to_humans;

function MessageContent({ message, settled }: { message: Message; settled: boolean }) {
  const targets = message.mentions.filter((m) => m.type !== "human").map((m) => m.name);
  if (message.approval) return <ApprovalRequest approval={message.approval} agentName={message.author_name} />;
  if (message.handoff && targets.length > 0) {
    const reason = message.content.replace(/^@\S+\s*/, "");
    return (
      <div className="tool-ui pt-1">
        <AgentHandoff className="max-w-2xl" from={message.author_name} to={targets[0]} reason={reason} carried={[]} settled={settled} />
      </div>
    );
  }
  return (
    <>
      {isChatter(message) && targets.length > 0 && <div className="chatter-label">to {targets.join(", ")}</div>}
      <RichText text={message.content} />
      {message.components?.length > 0 && <MessageComponents components={message.components} />}
    </>
  );
}

function MessageRow({
  message,
  state,
  compact,
  settled,
  openModal,
}: {
  message: Message;
  state: State;
  compact: boolean;
  settled: boolean;
  openModal: (m: Modal) => void;
}) {
  const agent = message.author_type === "agent" && message.author_id != null ? state.agents[message.author_id] : null;
  const chatter = isChatter(message);

  return (
    <div className={`msg ${compact ? "compact" : ""} ${chatter ? "chatter" : ""} msg-${message.author_type}`}>
      <div className="msg-gutter">
        {compact ? (
          <span className="msg-time-hover">{formatTime(message.created_at)}</span>
        ) : (
          <Avatar name={message.author_name} kind={message.author_type} role={agent?.role} />
        )}
      </div>
      <div className="msg-body">
        {!compact && (
          <div className="msg-header">
            <button
              className="msg-author"
              onClick={() => agent && openModal({ kind: "agent", agentId: agent.id })}
              disabled={!agent}
            >
              {message.author_name}
            </button>
            {agent && <span className="app-tag">{agent.role_label}</span>}
            {message.author_type === "external" && <span className="app-tag">via Band</span>}
            <span className="msg-time">{formatTime(message.created_at)}</span>
          </div>
        )}
        <MessageContent message={message} settled={settled} />
      </div>
    </div>
  );
}

function DraftRow({ draft, state, openModal }: { draft: Draft; state: State; openModal: (m: Modal) => void }) {
  const agent = state.agents[draft.agent_id];
  return (
    <div className="msg msg-agent msg-draft">
      <div className="msg-gutter">
        <Avatar name={agent.name} kind="agent" role={agent.role} />
      </div>
      <div className="msg-body">
        <div className="msg-header">
          <button className="msg-author" onClick={() => openModal({ kind: "agent", agentId: agent.id })}>
            {agent.name}
          </button>
          <span className="app-tag">{agent.role_label}</span>
          <span className="msg-time">writing…</span>
        </div>
        <RichText text={draft.content} />
      </div>
    </div>
  );
}

export default function ChannelView({
  state,
  dispatch,
  channel,
  showMembers,
  onToggleMembers,
  openModal,
  computers,
  computerOpen,
  openComputer,
}: {
  state: State;
  dispatch: Dispatch<Action>;
  channel: Channel;
  showMembers: boolean;
  onToggleMembers: () => void;
  openModal: (m: Modal) => void;
  computers: ChannelComputer[];
  computerOpen: boolean;
  openComputer: OpenComputer;
}) {
  const [showChatter, setShowChatter] = useState(() => localStorage.getItem("show-chatter") !== "0");
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const messages = state.messages[channel.id];
  const visible = (messages ?? []).filter((m) => showChatter || !isChatter(m));
  const hiddenCount = (messages?.length ?? 0) - visible.length;
  const drafts = Object.values(state.drafts[channel.id] ?? {}).filter((d) => state.agents[d.agent_id]);
  const draftLength = drafts.reduce((total, d) => total + d.content.length, 0);
  const working: { key: string; state: "working" | "waiting"; label: string }[] = [
    ...(state.typing[channel.id] ?? [])
      .filter((id) => state.agents[id])
      .map((id) => ({ key: `typing-${id}`, state: "working" as const, label: `${state.agents[id].name} is working…` })),
    ...Object.values(state.activity[channel.id] ?? {})
      .filter((t) => state.agents[t.agent_id])
      .map((t) => ({
        key: t.id,
        state: t.status === "running" ? "working" as const : "waiting" as const,
        label: `${state.agents[t.agent_id].name}: ${t.status === "queued" ? "waiting for its turn"
          : t.status === "waiting" ? "waiting for your approval" : t.progress}`,
      })),
  ];
  // A hand-off is settled once its teammate has posted after it.
  const settledHandoffs = new Set<number>();
  const laterAuthors = new Set<string>();
  for (const m of [...(messages ?? [])].reverse()) {
    if (m.handoff && m.mentions.some((t) => laterAuthors.has(t.name))) settledHandoffs.add(m.id);
    laterAuthors.add(m.author_name);
  }

  useEffect(() => localStorage.setItem("show-chatter", showChatter ? "1" : "0"), [showChatter]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [visible.length, working.length, draftLength]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const rows: ReactNode[] = [];
  let prev: Message | null = null;
  for (const m of visible) {
    const day = formatDay(m.created_at);
    if (!prev || formatDay(prev.created_at) !== day) {
      rows.push(
        <div key={`day-${m.id}`} className="day-divider">
          <span>{day}</span>
        </div>,
      );
      prev = null;
    }
    const compact =
      !!prev &&
      prev.author_type === m.author_type &&
      prev.author_name === m.author_name &&
      isChatter(prev) === isChatter(m) &&
      new Date(m.created_at).getTime() - new Date(prev.created_at).getTime() < GROUP_WINDOW_MS;
    rows.push(
      <MessageRow key={m.id} message={m} state={state} compact={compact} settled={settledHandoffs.has(m.id)} openModal={openModal} />,
    );
    prev = m;
  }

  const mentionNames = [
    ...Object.values(state.agents).map((a) => a.name),
    ...Object.values(state.users).map((u) => u.display_name),
  ];
  const agentCount = channel.agent_ids.length;
  const humanCount = channel.human_ids.length;
  const dmAgent = channel.dm_agent_id != null ? state.agents[channel.dm_agent_id] : null;
  const headerComputer = computers.find((c) => c.agent_working) ?? computers[0];

  return (
    <section className="channel">
      <header className="channel-header">
        {dmAgent ? (
          <div className="dm-title">
            <Avatar name={dmAgent.name} kind="agent" role={dmAgent.role} />
            <div>
              <div className="channel-title">{dmAgent.name}</div>
              <div className="channel-topic">
                {dmAgent.role_label} · {dmAgent.status.state} · only you can see this conversation
              </div>
            </div>
          </div>
        ) : (
          <div>
            <div className="channel-title"># {channel.name}</div>
            {channel.topic && <div className="channel-topic">{channel.topic}</div>}
          </div>
        )}
        <div className="channel-actions">
          <label className="toggle" title="Show messages agents send to each other">
            <input type="checkbox" checked={showChatter} onChange={(e) => setShowChatter(e.target.checked)} />
            Agent chatter
          </label>
          {headerComputer && !computerOpen && (
            <button
              className={`chip ${headerComputer.agent_working ? "computer-chip-live" : ""}`}
              title="Show the agent's computer"
              onClick={() => openComputer(headerComputer.agent_id, headerComputer.task_id)}
            >
              🖥 Computer{headerComputer.agent_working ? " · live" : ""}
            </button>
          )}
          {dmAgent ? (
            <button className="chip" onClick={() => openModal({ kind: "agent", agentId: dmAgent.id })}>
              ⚙ Settings
            </button>
          ) : (
            <button className={`chip ${showMembers ? "active" : ""}`} onClick={onToggleMembers}>
              👤 {humanCount} · 🤖 {agentCount}
            </button>
          )}
        </div>
      </header>

      <div className="messages" ref={scroller} onScroll={onScroll}>
        {!messages && <div className="muted center">Loading messages…</div>}
        {messages && messages.length === 0 && (
          <div className="channel-intro">
            {dmAgent ? (
              <>
                <h2>{dmAgent.name}</h2>
                <p>
                  This is your private conversation with <strong>{dmAgent.name}</strong> ({dmAgent.role_label}). Every
                  message goes straight to {dmAgent.name}, so no @mention is needed.
                </p>
              </>
            ) : (
              <>
                <h2># {channel.name}</h2>
                <p>
                  This is the start of <strong>#{channel.name}</strong>. Ask <span className="mention">@Master</span>{" "}
                  for something and it delegates to specialist helpers, or add visible teammates from the members
                  panel.
                </p>
              </>
            )}
          </div>
        )}
        <MentionNames names={mentionNames}>
          {rows}
          {drafts.map((d) => (
            <DraftRow key={d.task_id} draft={d} state={state} openModal={openModal} />
          ))}
        </MentionNames>
        {hiddenCount > 0 && (
          <button className="link hidden-note" onClick={() => setShowChatter(true)}>
            {hiddenCount} agent-to-agent message{hiddenCount === 1 ? "" : "s"} hidden. Show them
          </button>
        )}
      </div>

      <TaskPanel key={channel.id} channelId={channel.id} state={state} dispatch={dispatch} openComputer={openComputer} />

      <div className="tool-ui flex h-8 shrink-0 items-center gap-1.5 overflow-x-auto px-5 [scrollbar-width:none]" aria-live="polite">
        {working.map((item) => (
          <AgentStatus
            key={item.key}
            className="shrink-0 py-1 ps-2.5 pe-3"
            state={item.state}
            label={item.label}
            trailing={null}
          />
        ))}
      </div>

      <Composer state={state} channel={channel} />
    </section>
  );
}
