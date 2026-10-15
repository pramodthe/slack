import { useState } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Channel } from "../types";
import Avatar from "./Avatar";
import type { Modal } from "./Workspace";

export default function MembersPanel({
  state,
  channel,
  openModal,
}: {
  state: State;
  channel: Channel;
  openModal: (m: Modal) => void;
}) {
  const [error, setError] = useState("");
  const humans = channel.human_ids.map((id) => state.users[id]).filter(Boolean);
  const agents = channel.agent_ids.map((id) => state.agents[id]).filter(Boolean);
  const available = Object.values(state.agents).filter((a) => !channel.agent_ids.includes(a.id));
  const joined = channel.human_ids.includes(state.me.id);

  const run = (p: Promise<unknown>) => p.then(() => setError("")).catch((e) => setError((e as Error).message));

  return (
    <aside className="members">
      <div className="members-section">
        <div className="members-title">Agents in #{channel.name}</div>
        {agents.length === 0 && <div className="muted small">No agents yet.</div>}
        {agents.map((a) => (
          <div key={a.id} className="member">
            <Avatar name={a.name} kind="agent" role={a.role} size={28} />
            <button className="member-name" onClick={() => openModal({ kind: "agent", agentId: a.id })}>
              {a.name}
              <span className="muted small">{a.role_label}</span>
            </button>
            <span className={`dot dot-${a.status.state}`} title={a.status.error ?? a.status.state} />
            {!a.is_master && (
              <button
                className="icon-btn subtle"
                title="Remove from channel"
                onClick={() => run(api("DELETE", `/api/channels/${channel.id}/agents/${a.id}`))}
              >
                ×
              </button>
            )}
          </div>
        ))}
        {available.length > 0 ? (
          <select
            className="add-select"
            value=""
            onChange={(e) =>
              e.target.value && run(api("POST", `/api/channels/${channel.id}/agents`, { agent_id: Number(e.target.value) }))
            }
          >
            <option value="">+ Add an agent to this channel…</option>
            {available.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.role_label})
              </option>
            ))}
          </select>
        ) : (
          <button className="secondary small-btn" onClick={() => openModal({ kind: "new-agent" })}>
            + Create a new agent
          </button>
        )}
      </div>

      <div className="members-section">
        <div className="members-title">People</div>
        {humans.map((u) => (
          <div key={u.id} className="member">
            <Avatar name={u.display_name} kind="human" size={28} />
            <span className="member-name static">
              {u.display_name}
              <span className="muted small">@{u.username}</span>
            </span>
            <span className={`dot dot-${u.online ? "online" : "stopped"}`} />
          </div>
        ))}
        <button
          className="link small"
          onClick={() => run(api("POST", `/api/channels/${channel.id}/${joined ? "leave" : "join"}`))}
        >
          {joined ? "Leave channel" : "Join channel"}
        </button>
      </div>

      <div className="members-section muted small">
        {channel.band_room_id ? (
          <>
            This channel is the Band room <code>#{channel.name}</code> (<code>{channel.band_room_id.slice(0, 8)}</code>). The
            whole conversation is also visible at{" "}
            <a href="https://app.band.ai" target="_blank" rel="noreferrer">
              app.band.ai
            </a>
          </>
        ) : (
          "Not linked to Band yet: the room is created once the server connects to Band."
        )}
      </div>
      {error && <div className="error small">{error}</div>}
    </aside>
  );
}
