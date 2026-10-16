import { useState, type Dispatch } from "react";
import type { Action, State } from "../store";
import AgentModal from "./AgentModal";
import ChannelView from "./ChannelView";
import CreateAgentModal from "./CreateAgentModal";
import CreateChannelModal from "./CreateChannelModal";
import MembersPanel from "./MembersPanel";
import SetupBanner from "./SetupBanner";
import Sidebar from "./Sidebar";

export type Modal = { kind: "new-agent" } | { kind: "new-channel" } | { kind: "agent"; agentId: number } | null;

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
  const channel = state.activeChannelId != null ? state.channels[state.activeChannelId] : null;

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
            />
            {showMembers && !channel.is_dm && <MembersPanel state={state} channel={channel} openModal={setModal} />}
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
