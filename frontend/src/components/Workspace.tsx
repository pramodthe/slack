import { useEffect, useRef, useState, type Dispatch } from "react";
import { api } from "../api";
import type { Action, State } from "../store";
import type { ChannelComputer } from "../types";
import AgentModal from "./AgentModal";
import ChannelView from "./ChannelView";
import ComputerPanel from "./ComputerPanel";
import CreateAgentModal from "./CreateAgentModal";
import CreateChannelModal from "./CreateChannelModal";
import MembersPanel from "./MembersPanel";
import SetupBanner from "./SetupBanner";
import Sidebar from "./Sidebar";

export type Modal = { kind: "new-agent" } | { kind: "new-channel" } | { kind: "agent"; agentId: number } | null;
export type OpenComputer = (agentId: number, taskId: string) => void;

function useChannelComputers(channelId: number | null, onActivity: (computer: ChannelComputer) => void) {
  const [computers, setComputers] = useState<ChannelComputer[]>([]);
  const activity = useRef(onActivity);
  activity.current = onActivity;

  useEffect(() => {
    setComputers([]);
    if (channelId == null) return;
    const seen: Record<number, string> = {};
    let first = true;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await api<ChannelComputer[]>("GET", `/api/channels/${channelId}/computers`);
        if (stopped) return;
        setComputers(rows);
        for (const row of rows) {
          // A computer that changes while its agent works is in use right now; history from before loading is not.
          if (!first && row.agent_working && seen[row.agent_id] !== row.updated_at) activity.current(row);
          seen[row.agent_id] = row.updated_at;
        }
        first = false;
      } catch {
        // Polling resumes on the next tick; the computer panel reports its own errors.
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId]);

  return computers;
}

export default function Workspace({
  state,
  dispatch,
  connected,
  onSignOut,
}: {
  state: State;
  dispatch: Dispatch<Action>;
  connected: boolean;
  onSignOut: () => void;
}) {
  const [modal, setModal] = useState<Modal>(null);
  const [showMembers, setShowMembers] = useState(true);
  const [computer, setComputer] = useState<{ agentId: number; taskId: string } | null>(null);
  const dismissed = useRef(new Set<number>());
  const channel = state.activeChannelId != null ? state.channels[state.activeChannelId] : null;

  useEffect(() => {
    setComputer(null);
    dismissed.current = new Set();
  }, [channel?.id]);

  const computers = useChannelComputers(channel?.id ?? null, (row) => {
    if (!dismissed.current.has(row.agent_id)) {
      setComputer((current) => current ?? { agentId: row.agent_id, taskId: row.task_id });
    }
  });
  useEffect(() => {
    // Closing the panel hides it for the rest of that agent's work; its next task may open it again.
    for (const row of computers) if (!row.agent_working) dismissed.current.delete(row.agent_id);
  }, [computers]);

  const openComputer: OpenComputer = (agentId, taskId) => {
    dismissed.current.delete(agentId);
    setComputer({ agentId, taskId });
  };
  const closeComputer = () => {
    if (computer) dismissed.current.add(computer.agentId);
    setComputer(null);
  };
  const shown = computer && computers.find((c) => c.agent_id === computer.agentId);

  return (
    <div className="workspace">
      <Sidebar state={state} dispatch={dispatch} openModal={setModal} onSignOut={onSignOut} />
      <main className="main">
        <SetupBanner state={state} connected={connected} />
        {channel ? (
          <div className="channel-area">
            <ChannelView
              dispatch={dispatch}
              key={channel.id}
              state={state}
              channel={channel}
              showMembers={showMembers}
              onToggleMembers={() => setShowMembers((v) => !v)}
              openModal={setModal}
              computers={computers}
              computerOpen={computer !== null}
              openComputer={openComputer}
            />
            {computer ? (
              <ComputerPanel
                key={`${computer.agentId}-${channel.id}`}
                taskId={computer.taskId}
                agent={state.agents[computer.agentId]}
                working={shown?.agent_working ?? false}
                others={computers
                  .filter((c) => c.agent_id !== computer.agentId && state.agents[c.agent_id])
                  .map((c) => ({ agent: state.agents[c.agent_id], working: c.agent_working }))}
                onSwitch={(agentId) => {
                  const next = computers.find((c) => c.agent_id === agentId);
                  if (next) openComputer(next.agent_id, next.task_id);
                }}
                onClose={closeComputer}
              />
            ) : (
              showMembers && !channel.is_dm && <MembersPanel key={`members-${channel.id}`} state={state} channel={channel} openModal={setModal} />
            )}
          </div>
        ) : (
          <div className="empty">Pick or create a channel to get started.</div>
        )}
      </main>
      {modal?.kind === "new-channel" && (
        <CreateChannelModal
          onClose={() => setModal(null)}
          onCreated={(id) => {
            setModal(null);
            dispatch({ type: "select_channel", channelId: id });
          }}
        />
      )}
      {modal?.kind === "new-agent" && (
        <CreateAgentModal state={state} channel={channel?.is_dm ? null : channel} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "agent" && state.agents[modal.agentId] && (
        <AgentModal state={state} agent={state.agents[modal.agentId]} onClose={() => setModal(null)} />
      )}
    </div>
  );
}
