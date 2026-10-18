import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type Dispatch } from "react";
import { policyBlock, type State, type Action } from "../store";
import type { Agent, Channel, ChannelComputer, Draft, Message } from "../types";
import { cn } from "@/lib/utils";
import Avatar from "./Avatar";
import Composer from "./Composer";
import TaskPanel from "./TaskPanel";
import RichText, { MentionNames, SourcesPill, linksIn, mentionClass } from "./RichText";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import MessageComponents from "./MessageComponents";
import ApprovalRequest from "./ApprovalRequest";
import { AgentHandoff } from "./assistant-ui/elements/agent-handoff";
import { AgentStatus } from "./assistant-ui/elements/agent-status";
import type { Modal, OpenComputer } from "./Workspace";

const GROUP_WINDOW_MS = 5 * 60 * 1000;
const titleClass = "truncate text-[18px] font-black";
const topicClass = "text-muted-foreground truncate text-[13px]";

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

function MessageContent({ message, settled, state }: { message: Message; settled: boolean; state: State }) {
  const targets = message.mentions.filter((m) => m.type !== "human").map((m) => m.name);
  if (message.approval) {
    const blocked = policyBlock(state, state.channels[message.channel_id], "approvers");
    return <ApprovalRequest approval={message.approval} agentName={message.author_name} blocked={blocked} />;
  }
  if (message.handoff && targets.length > 0) {
    const reason = message.content.replace(/^@\S+\s*/, "");
    return (
      <div className="pt-1">
        <AgentHandoff className="max-w-2xl" from={message.author_name} to={targets[0]} reason={reason} carried={[]} settled={settled} />
      </div>
    );
  }
  const links = message.author_type === "agent" && message.to_humans ? linksIn(message.content) : [];
  return (
    <>
      {isChatter(message) && targets.length > 0 && (
        <div className="text-chatter mb-0.5 text-xs font-bold">to {targets.join(", ")}</div>
      )}
      <RichText text={message.content} />
      {message.components?.length > 0 && <MessageComponents components={message.components} />}
      {links.length > 1 && <SourcesPill links={links} />}
    </>
  );
}

const rowClass = "group flex gap-2.5 px-5 py-1.5 hover:bg-muted";
const caretClass =
  "[&_[data-slot=rich-text]>:last-child>:last-child]:after:bg-muted-foreground [&_[data-slot=rich-text]>:last-child>:last-child]:after:animate-caret [&_[data-slot=rich-text]>:last-child>:last-child]:after:ms-0.5 [&_[data-slot=rich-text]>:last-child>:last-child]:after:inline-block [&_[data-slot=rich-text]>:last-child>:last-child]:after:h-[1em] [&_[data-slot=rich-text]>:last-child>:last-child]:after:w-[7px] [&_[data-slot=rich-text]>:last-child>:last-child]:after:align-text-bottom [&_[data-slot=rich-text]>:last-child>:last-child]:after:content-['']";

function AuthorLine({
  name,
  agent,
  tag,
  time,
  writing = false,
  openModal,
}: {
  name: string;
  agent: Agent | null;
  tag: string | null;
  time: string;
  writing?: boolean;
  openModal: (m: Modal) => void;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <button
        className="font-black enabled:hover:underline disabled:cursor-default"
        onClick={() => agent && openModal({ kind: "agent", agentId: agent.id })}
        disabled={!agent}
      >
        {name}
      </button>
      {tag && <Badge variant="tag">{tag}</Badge>}
      <span className={cn("text-muted-foreground text-xs", writing && "italic")}>{time}</span>
    </div>
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
    <div className={cn(rowClass, compact && "py-px")}>
      <div className="w-9 shrink-0">
        {compact ? (
          <span className="text-muted-foreground invisible block pt-[3px] text-right text-[11px] group-hover:visible">
            {formatTime(message.created_at)}
          </span>
        ) : (
          <Avatar name={message.author_name} kind={message.author_type} role={agent?.role} />
        )}
      </div>
      <div
        className={cn(
          "min-w-0 flex-1",
          chatter && "border-l-[3px] border-[#d9cfe0] pl-2.5 text-[14px] text-[#454245]",
          message.author_type === "system" && "text-muted-foreground italic",
        )}
      >
        {!compact && (
          <AuthorLine
            name={message.author_name}
            agent={agent}
            tag={agent ? agent.role_label : message.author_type === "external" ? "via Band" : null}
            time={formatTime(message.created_at)}
            openModal={openModal}
          />
        )}
        <MessageContent message={message} settled={settled} state={state} />
      </div>
    </div>
  );
}

function DraftRow({ draft, state, openModal }: { draft: Draft; state: State; openModal: (m: Modal) => void }) {
  const agent = state.agents[draft.agent_id];
  return (
    <div className={rowClass}>
      <div className="w-9 shrink-0">
        <Avatar name={agent.name} kind="agent" role={agent.role} />
      </div>
      <div className={cn("min-w-0 flex-1", caretClass)}>
        <AuthorLine name={agent.name} agent={agent} tag={agent.role_label} time="writing…" writing openModal={openModal} />
        <RichText text={draft.content} streaming />
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
        <div
          key={`day-${m.id}`}
          className="before:border-border after:border-border mt-3.5 mb-1.5 flex items-center text-[13px] font-bold before:flex-1 before:border-t after:flex-1 after:border-t"
        >
          <span className="border-border rounded-xl border px-3 py-0.5">{day}</span>
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
    <section className="flex min-w-0 flex-1 flex-col">
      <header className="border-border flex min-h-[58px] items-center justify-between gap-3 border-b px-5 py-2.5">
        {dmAgent ? (
          <div className="flex min-w-0 items-center gap-2.5">
            <Avatar name={dmAgent.name} kind="agent" role={dmAgent.role} />
            <div className="min-w-0">
              <div className={titleClass}>{dmAgent.name}</div>
              <div className={topicClass}>
                {dmAgent.role_label} · {dmAgent.status.state} · only you can see this conversation
              </div>
            </div>
          </div>
        ) : (
          <div className="min-w-0">
            <div className={titleClass}># {channel.name}</div>
            {channel.topic && <div className={topicClass}>{channel.topic}</div>}
          </div>
        )}
        <div className="flex shrink-0 items-center gap-3">
          <label
            className="text-muted-foreground flex cursor-pointer items-center gap-1.5 text-[13px]"
            title="Show messages agents send to each other"
          >
            <Checkbox checked={showChatter} onCheckedChange={(checked) => setShowChatter(checked === true)} />
            Agent chatter
          </label>
          {headerComputer && !computerOpen && (
            <Button
              variant="chip"
              size="chip"
              className={cn(headerComputer.agent_working && "border-[#1d9b5f] font-bold text-[#1d9b5f]")}
              title="Show the agent's computer"
              onClick={() => openComputer(headerComputer.agent_id, headerComputer.task_id)}
            >
              🖥 Computer{headerComputer.agent_working ? " · live" : ""}
            </Button>
          )}
          {dmAgent ? (
            <Button variant="chip" size="chip" onClick={() => openModal({ kind: "agent", agentId: dmAgent.id })}>
              ⚙ Settings
            </Button>
          ) : (
            <Button
              variant="chip"
              size="chip"
              className={cn(showMembers && "bg-muted")}
              aria-pressed={showMembers}
              onClick={onToggleMembers}
            >
              👤 {humanCount} · 🤖 {agentCount}
            </Button>
          )}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto pt-3 pb-1" ref={scroller} onScroll={onScroll}>
        {!messages && <div className="text-muted-foreground p-6 text-center">Loading messages…</div>}
        {messages && messages.length === 0 && (
          <div className="flex flex-col gap-1.5 p-5">
            {dmAgent ? (
              <>
                <h2 className="text-[22px] font-black">{dmAgent.name}</h2>
                <p>
                  This is your private conversation with <strong>{dmAgent.name}</strong> ({dmAgent.role_label}). Every
                  message goes straight to {dmAgent.name}, so no @mention is needed.
                </p>
              </>
            ) : (
              <>
                <h2 className="text-[22px] font-black"># {channel.name}</h2>
                <p>
                  This is the start of <strong>#{channel.name}</strong>. Ask <span className={mentionClass}>@Master</span>{" "}
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
          <Button variant="link" size="inline" className="mx-5 my-2 text-[13px]" onClick={() => setShowChatter(true)}>
            {hiddenCount} agent-to-agent message{hiddenCount === 1 ? "" : "s"} hidden. Show them
          </Button>
        )}
      </div>

      <TaskPanel key={channel.id} channelId={channel.id} state={state} dispatch={dispatch} openComputer={openComputer} />

      <div className="relative flex h-8 shrink-0 items-center gap-1.5 overflow-x-auto px-5 [scrollbar-width:none]" aria-live="polite">
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
