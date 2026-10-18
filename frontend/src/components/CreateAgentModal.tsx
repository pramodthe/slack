import { useId, useState, type FormEvent } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Channel } from "../types";
import Avatar from "./Avatar";
import Field from "./Field";
import ModalShell, { actionsClass, formClass } from "./ModalShell";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Input } from "./ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select";
import { Textarea } from "./ui/textarea";

export default function CreateAgentModal({
  state,
  channel,
  onClose,
}: {
  state: State;
  channel: Channel | null;
  onClose: () => void;
}) {
  const id = useId();
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
      <form className={formClass} onSubmit={submit}>
        {master && (
          <div className="bg-muted border-border rounded-lg border px-3.5 py-3 text-sm">
            Tip: you can also just ask <strong>@{master.name}</strong> in a channel. It creates the specialists it needs
            and puts them to work.
          </div>
        )}

        <div className="text-sm font-bold" id={`${id}-role`}>
          Role
        </div>
        <div role="radiogroup" aria-labelledby={`${id}-role`} className="narrow:grid-cols-2 -mt-1.5 grid grid-cols-3 gap-2">
          {state.roles.map((r) => (
            <button
              type="button"
              role="radio"
              aria-checked={r.id === role}
              key={r.id}
              className="border-border aria-checked:border-active aria-checked:ring-active/25 flex items-start gap-2 rounded-lg border bg-white p-2.5 text-left aria-checked:ring-2"
              onClick={() => setRole(r.id)}
            >
              <Avatar name={r.label} kind="agent" role={r.id} size={28} />
              <div className="min-w-0">
                <div className="font-black">{r.label}</div>
                <div className="text-muted-foreground text-[12.5px]">{r.description}</div>
              </div>
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field id={`${id}-name`} label="Name" hint="(how everyone @mentions it)">
            <Input
              id={`${id}-name`}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={selectedRole?.label}
              minLength={3}
              maxLength={40}
            />
          </Field>
          <Field id={`${id}-model`} label="Model">
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger id={`${id}-model`} className="w-full">
                <SelectValue placeholder="Choose a model" />
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

        <Field id={`${id}-instructions`} label="Extra instructions" hint="(optional)">
          <Textarea
            id={`${id}-instructions`}
            rows={4}
            className="min-h-24"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder={selectedRole?.instructions || "Describe exactly what this agent should do."}
          />
        </Field>

        {channel && (
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={addToChannel} onCheckedChange={(checked) => setAddToChannel(checked === true)} />
            Add to #{channel.name}
          </label>
        )}

        <p className="text-muted-foreground text-[12.5px]">
          The agent is registered on Band under your organization and starts right away.
        </p>

        {error && <div className="text-destructive text-[13.5px]">{error}</div>}
        <div className={actionsClass}>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || !state.band.online}>{busy ? "Registering on Band…" : "Create agent"}</Button>
        </div>
      </form>
    </ModalShell>
  );
}
