import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { Channel } from "../types";
import ModalShell from "./ModalShell";

export default function CreateChannelModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: number) => void;
}) {
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const channel = await api<Channel>("POST", "/api/channels", { name, topic });
      onCreated(channel.id);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <ModalShell title="Create a channel" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. launch-plan" autoFocus required />
        </label>
        <label>
          Topic <span className="muted">(optional)</span>
          <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="What is this channel about?" />
        </label>
        {error && <div className="error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Create</button>
        </div>
      </form>
    </ModalShell>
  );
}
