import { useState } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Agent } from "../types";
import Avatar from "./Avatar";
import ModalShell from "./ModalShell";

const STATE_LABELS: Record<string, string> = {
  online: "Online",
  starting: "Starting…",
  stopped: "Stopped",
  error: "Error",
};

export default function AgentModal({ state, agent, onClose }: { state: State; agent: Agent; onClose: () => void }) {
  const [role, setRole] = useState(agent.role);
  const [model, setModel] = useState(agent.model);
  const [instructions, setInstructions] = useState(agent.instructions);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dirty = role !== agent.role || model !== agent.model || instructions !== agent.instructions;
  const channels = Object.values(state.channels).filter((c) => c.agent_ids.includes(agent.id));
  const creator = agent.created_by_agent
    ? state.agents[agent.created_by_agent]?.name
    : agent.created_by
      ? state.users[agent.created_by]?.display_name
      : null;

  const run = async (fn: () => Promise<unknown>, close = false) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      if (close) onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title={agent.name} onClose={onClose} wide>
      <div className="agent-summary">
        <Avatar name={agent.name} kind="agent" role={agent.role} size={48} />
        <div>
          <div className="role-name">
            {agent.name} <span className="app-tag">{agent.role_label}</span>
          </div>
          <div className="muted small">
            {agent.handle ? `@${agent.handle}` : agent.band_agent_id}
          </div>
          <div className="small">
            <span className={`dot dot-${agent.status.state}`} /> {STATE_LABELS[agent.status.state] ?? agent.status.state}
            {agent.status.error && <span className="error inline"> · {agent.status.error}</span>}
          </div>
          {channels.length > 0 && (
            <div className="muted small">In {channels.map((c) => `#${c.name}`).join(", ")}</div>
          )}
          {agent.is_master ? (
            <div className="muted small">Orchestrator: creates and directs the other agents. Joins every channel.</div>
          ) : (
            creator && <div className="muted small">Created by {creator}</div>
          )}
        </div>
      </div>

      <div className="form">
        <div className="row">
          {!agent.is_master && (
            <label>
              Role
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {state.roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {state.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          Extra instructions
          <textarea rows={5} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
        </label>
        {error && <div className="error">{error}</div>}
        <div className="form-actions spread">
          {agent.is_master ? (
            <span />
          ) : (
            <button
              className="danger"
              disabled={busy}
              onClick={() =>
                confirm(`Delete ${agent.name}? This also deletes the agent and its history on Band.`) &&
                run(() => api("DELETE", `/api/agents/${agent.id}`), true)
              }
            >
              Delete agent
            </button>
          )}
          <div className="form-actions">
            <button className="secondary" disabled={busy} onClick={() => run(() => api("POST", `/api/agents/${agent.id}/restart`))}>
              Restart
            </button>
            <button
              className="primary"
              disabled={busy || !dirty}
              onClick={() => run(() => api("PATCH", `/api/agents/${agent.id}`, { ...(agent.is_master ? {} : { role }), model, instructions }), true)}
            >
              Save and restart
            </button>
          </div>
        </div>
      </div>
    </ModalShell>
  );
}
