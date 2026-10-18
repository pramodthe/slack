import { useState, type Dispatch, type ReactNode } from "react";
import { PlusIcon, SettingsIcon } from "lucide-react";
import { api } from "../api";
import type { Action, State } from "../store";
import type { Agent, Channel } from "../types";
import { cn } from "@/lib/utils";
import StatusDot from "./StatusDot";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import type { Modal } from "./Workspace";

const navItem =
  "mx-2 flex w-[calc(100%-16px)] items-center gap-2 overflow-hidden rounded-md px-2.5 py-[5px] text-left text-[15px] whitespace-nowrap";
const navButton = (active: boolean, unread: boolean) =>
  cn(navItem, "hover:bg-aubergine-hover", unread && "font-black text-white", active && "bg-active hover:bg-active text-white");
const iconButton = "text-sidebar-text size-6 hover:bg-white/15 hover:text-white";

function SidebarAction({ label, onClick, className, children }: {
  label: string;
  onClick: () => void;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-xs" className={cn(iconButton, className)} aria-label={label} onClick={onClick}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

function UnreadBadge({ count, className }: { count: number; className?: string }) {
  return (
    <Badge variant="destructive" className={cn("ms-auto rounded-full px-[7px] py-px text-[11px] font-normal", className)}>
      {count}
    </Badge>
  );
}

export default function Sidebar({
  state,
  dispatch,
  openModal,
  onSignOut,
}: {
  state: State;
  dispatch: Dispatch<Action>;
  openModal: (m: Modal) => void;
  onSignOut: () => void;
}) {
  const [opening, setOpening] = useState<number | null>(null);
  const [dmError, setDmError] = useState("");
  const channels = Object.values(state.channels)
    .filter((c) => !c.is_dm)
    .sort((a, b) => a.name.localeCompare(b.name));
  const agents = Object.values(state.agents).sort((a, b) => a.name.localeCompare(b.name));
  const dms = Object.fromEntries(
    Object.values(state.channels)
      .filter((c) => c.is_dm && c.dm_agent_id !== null)
      .map((c) => [c.dm_agent_id, c]),
  ) as Record<number, Channel>;

  const openDm = async (agent: Agent) => {
    setDmError("");
    const existing = dms[agent.id];
    if (existing) {
      dispatch({ type: "select_channel", channelId: existing.id });
      return;
    }
    setOpening(agent.id);
    try {
      const channel = await api<Channel>("POST", `/api/agents/${agent.id}/dm`);
      dispatch({ type: "event", event: { type: "channel", channel } });
      dispatch({ type: "select_channel", channelId: channel.id });
    } catch (err) {
      setDmError(`Couldn't open a chat with ${agent.name}: ${(err as Error).message}`);
    } finally {
      setOpening(null);
    }
  };
  const people = Object.values(state.users).sort(
    (a, b) => Number(b.online) - Number(a.online) || a.display_name.localeCompare(b.display_name),
  );
  const sectionTitle = "flex items-center justify-between py-1 pr-3 pl-[18px] text-[13.5px] font-bold";

  return (
    <aside className="bg-aubergine text-sidebar-text narrow:w-[210px] flex w-[260px] shrink-0 flex-col">
      <div className="border-aubergine-line border-b px-4 pt-3.5 pb-3">
        <div className="text-lg font-black text-white">Band Slack</div>
        <div className="mt-1.5 inline-flex items-center gap-1.5 text-xs" title={state.band.error ?? ""}>
          <StatusDot state={state.band.online ? "online" : "error"} />
          {state.band.online ? `Band: ${state.band.owner_name}` : "Band offline"}
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        <div className="mb-4">
          <div className={sectionTitle}>
            Channels
            <SidebarAction label="New channel" onClick={() => openModal({ kind: "new-channel" })}>
              <PlusIcon />
            </SidebarAction>
          </div>
          {channels.map((c) => {
            const unread = state.unread[c.id] ?? 0;
            return (
              <button
                key={c.id}
                className={navButton(c.id === state.activeChannelId, unread > 0)}
                onClick={() => dispatch({ type: "select_channel", channelId: c.id })}
              >
                <span className="opacity-70">#</span> {c.name}
                {unread > 0 && <UnreadBadge count={unread} />}
              </button>
            );
          })}
        </div>

        <div className="mb-4">
          <div className={sectionTitle}>
            Agents
            <SidebarAction label="New agent" onClick={() => openModal({ kind: "new-agent" })}>
              <PlusIcon />
            </SidebarAction>
          </div>
          {agents.map((a) => {
            const dm = dms[a.id];
            const unread = dm ? (state.unread[dm.id] ?? 0) : 0;
            return (
              <div key={a.id} className="group relative">
                <button
                  className={cn(navButton(!!dm && dm.id === state.activeChannelId, unread > 0), "pr-[34px]")}
                  title={`Chat privately with ${a.name}`}
                  disabled={opening === a.id}
                  onClick={() => openDm(a)}
                >
                  <StatusDot state={a.status.state} /> {a.name}
                  <span className={cn("ms-auto text-[11px] opacity-70", a.is_master && "text-away font-bold opacity-100")}>
                    {a.role_label}
                  </span>
                  {unread > 0 && <UnreadBadge count={unread} className="ms-1.5" />}
                </button>
                <SidebarAction
                  label={`${a.name} settings`}
                  className="absolute top-1/2 right-3 -translate-y-1/2 opacity-0 group-hover:opacity-80 focus-visible:opacity-80"
                  onClick={() => openModal({ kind: "agent", agentId: a.id })}
                >
                  <SettingsIcon />
                </SidebarAction>
              </div>
            );
          })}
          {dmError && <div className="text-destructive mx-[18px] my-1 text-[12.5px]">{dmError}</div>}
          {agents.length === 0 && (
            <button className={cn(navItem, "hover:bg-aubergine-hover text-[#a995a9]")} onClick={() => openModal({ kind: "new-agent" })}>
              + Add your first agent
            </button>
          )}
        </div>

        <div className="mb-4">
          <div className={sectionTitle}>People</div>
          {people.map((u) => (
            <div key={u.id} className={navItem}>
              <StatusDot state={u.online ? "online" : "stopped"} /> {u.display_name}
              {u.id === state.me.id && <span className="ms-auto text-[11px] opacity-70">you</span>}
            </div>
          ))}
        </div>
      </nav>

      <div className="border-aubergine-line flex justify-between gap-2 border-t px-4 py-3 text-[13px]">
        <span>
          Signed in as <strong className="text-white">{state.me.display_name}</strong>
        </span>
        <Button variant="link" size="inline" className="text-sidebar-text text-[13px]" onClick={onSignOut}>
          Sign out
        </Button>
      </div>
    </aside>
  );
}
