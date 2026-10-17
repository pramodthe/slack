import { useId, useState } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Agent } from "../types";
import Avatar from "./Avatar";
import Field from "./Field";
import ModalShell, { actionsClass, formClass } from "./ModalShell";
import StatusDot from "./StatusDot";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select";
import { Textarea } from "./ui/textarea";

const hint = "text-muted-foreground";

const STATE_LABELS: Record<string, string> = {
  online: "Online",
  starting: "Starting…",
  stopped: "Stopped",
  error: "Error",
};

export default function AgentModal({ state, agent, onClose }: { state: State; agent: Agent; onClose: () => void }) {
  const id = useId();
  const [role, setRole] = useState(agent.role);
  const [model, setModel] = useState(agent.model);
  const [instructions, setInstructions] = useState(agent.instructions);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dirty = role !== agent.role || model !== agent.model || instructions !== agent.instructions;
  const channels = Object.values(state.channels).filter((c) => !c.is_dm && c.agent_ids.includes(agent.id));
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
      <div className="mb-[18px] flex items-center gap-3.5">
        <Avatar name={agent.name} kind="agent" role={agent.role} size={48} />
        <div className="flex min-w-0 flex-col gap-[3px] text-[12.5px]">
          <div className="text-[15px] font-black">
            {agent.name} <Badge variant="tag">{agent.role_label}</Badge>
          </div>
          <div className={hint}>{agent.handle ? `@${agent.handle}` : agent.band_agent_id}</div>
          <div className="flex items-center gap-1.5">
            <StatusDot state={agent.status.state} /> {STATE_LABELS[agent.status.state] ?? agent.status.state}
            {agent.status.error && <span className="text-destructive"> · {agent.status.error}</span>}
          </div>
          {channels.length > 0 && <div className={hint}>In {channels.map((c) => `#${c.name}`).join(", ")}</div>}
          {agent.is_master ? (
            <div className={hint}>Orchestrator: creates and directs the other agents. Joins every channel.</div>
          ) : (
            creator && <div className={hint}>Created by {creator}</div>
          )}
        </div>
      </div>

      <div className={formClass}>
        <div className="grid grid-cols-2 gap-3">
          {!agent.is_master && (
            <Field id={`${id}-role`} label="Role">
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger id={`${id}-role`} className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper">
                  {state.roles.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}
          <Field id={`${id}-model`} label="Model">
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger id={`${id}-model`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {state.models.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </div>
        <Field id={`${id}-instructions`} label="Extra instructions">
          <Textarea
            id={`${id}-instructions`}
            rows={5}
            className="min-h-28"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </Field>
        {error && <div className="text-destructive text-[13.5px]">{error}</div>}
        <div className="flex justify-between gap-2">
          {agent.is_master ? (
            <span />
          ) : (
            <Button
              variant="danger"
              disabled={busy}
              onClick={() =>
                confirm(`Delete ${agent.name}? This also deletes the agent and its history on Band.`) &&
                run(() => api("DELETE", `/api/agents/${agent.id}`), true)
              }
            >
              Delete agent
            </Button>
          )}
          <div className={actionsClass}>
            <Button variant="secondary" disabled={busy} onClick={() => run(() => api("POST", `/api/agents/${agent.id}/restart`))}>
              Restart
            </Button>
            <Button
              disabled={busy || !dirty}
              onClick={() => run(() => api("PATCH", `/api/agents/${agent.id}`, { ...(agent.is_master ? {} : { role }), model, instructions }), true)}
            >
              Save and restart
            </Button>
          </div>
        </div>
      </div>
    </ModalShell>
  );
}
