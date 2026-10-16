import { useState, type Dispatch } from "react";
import { api } from "../api";
import type { Action, State } from "../store";
import type { Agent, Channel } from "../types";
import type { Modal } from "./Workspace";

function StatusDot({ state }: { state: string }) {
  return <span className={`dot dot-${state}`} />;
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

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="workspace-name">Band Slack</div>
        <div className={`hub-pill ${state.band.online ? "on" : "off"}`} title={state.band.error ?? ""}>
          <StatusDot state={state.band.online ? "online" : "error"} />{" "}
          {state.band.online ? `Band: ${state.band.owner_name}` : "Band offline"}
        </div>
      </div>

      <nav className="sidebar-scroll">
        <div className="section">
          <div className="section-title">
            Channels
            <button className="icon-btn" title="New channel" onClick={() => openModal({ kind: "new-channel" })}>
              +
            </button>
          </div>
          {channels.map((c) => {
            const unread = state.unread[c.id] ?? 0;
            return (
              <button
                key={c.id}
                className={`nav-item ${c.id === state.activeChannelId ? "active" : ""} ${unread ? "unread" : ""}`}
                onClick={() => dispatch({ type: "select_channel", channelId: c.id })}
              >
                <span className="hash">#</span> {c.name}
                {unread > 0 && <span className="badge">{unread}</span>}
              </button>
            );
          })}
        </div>

        <div className="section">
          <div className="section-title">
            Agents
            <button className="icon-btn" title="New agent" onClick={() => openModal({ kind: "new-agent" })}>
              +
            </button>
          </div>
          {agents.map((a) => {
            const dm = dms[a.id];
            const unread = dm ? (state.unread[dm.id] ?? 0) : 0;
            return (
              <div key={a.id} className="agent-row">
                <button
                  className={`nav-item ${dm && dm.id === state.activeChannelId ? "active" : ""} ${unread ? "unread" : ""}`}
                  title={`Chat privately with ${a.name}`}
                  disabled={opening === a.id}
                  onClick={() => openDm(a)}
                >
                  <StatusDot state={a.status.state} /> {a.name}
                  <span className={`role-tag ${a.is_master ? "master" : ""}`}>{a.role_label}</span>
                  {unread > 0 && <span className="badge">{unread}</span>}
                </button>
                <button
                  className="icon-btn agent-settings"
                  title={`${a.name} settings`}
                  aria-label={`${a.name} settings`}
                  onClick={() => openModal({ kind: "agent", agentId: a.id })}
                >
                  ⚙
                </button>
              </div>
            );
          })}
          {dmError && <div className="error small sidebar-error">{dmError}</div>}
          {agents.length === 0 && (
            <button className="nav-item add" onClick={() => openModal({ kind: "new-agent" })}>
              + Add your first agent
            </button>
          )}
        </div>

        <div className="section">
          <div className="section-title">People</div>
          {people.map((u) => (
            <div key={u.id} className="nav-item static">
              <StatusDot state={u.online ? "online" : "stopped"} /> {u.display_name}
              {u.id === state.me.id && <span className="role-tag">you</span>}
            </div>
          ))}
        </div>
      </nav>

      <div className="sidebar-footer">
        <span>
          Signed in as <strong>{state.me.display_name}</strong>
        </span>
        <button className="link light" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </aside>
  );
}
