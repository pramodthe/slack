import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Channel } from "../types";
import Avatar from "./Avatar";
import ModalShell from "./ModalShell";

export default function CreateAgentModal({
  state,
  channel,
  onClose,
}: {
  state: State;
  channel: Channel | null;
  onClose: () => void;
}) {
  const [role, setRole] = useState("researcher");
  const [name, setName] = useState("");
  const [model, setModel] = useState(state.models[1]?.id ?? state.models[0]?.id ?? "");
  const [instructions, setInstructions] = useState("");
  const [addToChannel, setAddToChannel] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selectedRole = state.roles.find((r) => r.id === role);
  const master = Object.values(state.agents).find((a) => a.is_master);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("POST", "/api/agents", {
        name: name.trim() || selectedRole?.label,
        role,
        model,
        instructions,
        channel_id: addToChannel && channel ? channel.id : null,
      });
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title="Add an agent" onClose={onClose} wide>
      <form className="form" onSubmit={submit}>
        {master && (
          <div className="callout">
            Tip: you can also just ask <strong>@{master.name}</strong> in a channel. It creates the specialists it needs
            and puts them to work.
          </div>
        )}

        <div className="field-label">Role</div>
        <div className="role-grid">
          {state.roles.map((r) => (
            <button
              type="button"
              key={r.id}
              className={`role-card ${r.id === role ? "active" : ""}`}
              onClick={() => setRole(r.id)}
            >
              <Avatar name={r.label} kind="agent" role={r.id} size={28} />
              <div>
                <div className="role-name">{r.label}</div>
                <div className="muted small">{r.description}</div>
              </div>
            </button>
          ))}
        </div>

        <div className="row">
          <label>
            Name <span className="muted">(how everyone @mentions it)</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={selectedRole?.label}
              minLength={3}
              maxLength={40}
            />
          </label>
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
          Extra instructions <span className="muted">(optional)</span>
          <textarea
            rows={4}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder={selectedRole?.instructions || "Describe exactly what this agent should do."}
          />
        </label>

        {channel && (
          <label className="checkbox">
            <input type="checkbox" checked={addToChannel} onChange={(e) => setAddToChannel(e.target.checked)} />
            Add to #{channel.name}
          </label>
        )}

        <p className="muted small">The agent is registered on Band under your organization and starts right away.</p>

        {error && <div className="error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy || !state.band.online}>
            {busy ? "Registering on Band…" : "Create agent"}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
