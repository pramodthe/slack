import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api } from "../api";
import type { State } from "../store";
import type { Channel } from "../types";
import Avatar from "./Avatar";

interface Candidate {
  key: string;
  name: string;
  kind: "agent" | "human";
  detail: string;
  role?: string;
  inChannel: boolean;
}

export default function Composer({ state, channel }: { state: State; channel: Channel }) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const master = Object.values(state.agents).find((a) => a.is_master);

  const candidates = useMemo<Candidate[]>(() => {
    if (query === null) return [];
    const q = query.toLowerCase();
    const agents: Candidate[] = Object.values(state.agents).map((a) => ({
      key: `a${a.id}`,
      name: a.name,
      kind: "agent",
      detail: a.role_label,
      role: a.role,
      inChannel: channel.agent_ids.includes(a.id),
    }));
    const humans: Candidate[] = Object.values(state.users)
      .filter((u) => u.id !== state.me.id)
      .map((u) => ({
        key: `h${u.id}`,
        name: u.display_name,
        kind: "human",
        detail: `@${u.username}`,
        inChannel: channel.human_ids.includes(u.id),
      }));
    return [...agents, ...humans]
      .filter((c) => c.name.toLowerCase().includes(q))
      .sort((a, b) => Number(b.inChannel) - Number(a.inChannel) || Number(a.kind !== "agent") - Number(b.kind !== "agent"))
      .slice(0, 8);
  }, [query, state.agents, state.users, state.me.id, channel]);

  const updateQuery = (value: string, caret: number) => {
    const match = /(^|\s)@([^\s@]*)$/.exec(value.slice(0, caret));
    setQuery(match ? match[2] : null);
    setHighlight(0);
  };

  const pick = (c: Candidate) => {
    const el = input.current;
    const caret = el?.selectionStart ?? text.length;
    const before = text.slice(0, caret).replace(/@([^\s@]*)$/, `@${c.name} `);
    const next = before + text.slice(caret);
    setText(next);
    setQuery(null);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(before.length, before.length);
    });
  };

  const send = async () => {
    const content = text.trim();
    if (!content || sending) return;
    setSending(true);
    setError("");
    try {
      await api("POST", `/api/channels/${channel.id}/messages`, { content });
      setText("");
      setQuery(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSending(false);
      input.current?.focus();
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (candidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        return setHighlight((h) => (h + 1) % candidates.length);
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        return setHighlight((h) => (h - 1 + candidates.length) % candidates.length);
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        return pick(candidates[highlight]);
      }
      if (e.key === "Escape") return setQuery(null);
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="composer-wrap">
      {candidates.length > 0 && (
        <div className="mention-menu">
          {candidates.map((c, i) => (
            <button
              key={c.key}
              className={`mention-option ${i === highlight ? "active" : ""}`}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(c);
              }}
              onMouseEnter={() => setHighlight(i)}
            >
              <Avatar name={c.name} kind={c.kind} role={c.role} size={22} />
              <span className="mention-name">{c.name}</span>
              <span className="muted">{c.detail}</span>
              {!c.inChannel && <span className="muted small">{c.kind === "agent" ? "will join" : "not in channel"}</span>}
            </button>
          ))}
        </div>
      )}
      <div className="composer">
        <textarea
          ref={input}
          rows={1}
          value={text}
          placeholder={
            master
              ? `Message #${channel.name}. Ask @${master.name} to build a team, or @mention anyone`
              : `Message #${channel.name}. Type @ to mention an agent or teammate`
          }
          onChange={(e) => {
            setText(e.target.value);
            updateQuery(e.target.value, e.target.selectionStart);
          }}
          onClick={(e) => updateQuery(text, e.currentTarget.selectionStart)}
          onKeyDown={onKeyDown}
          autoFocus
        />
        <button className="send" onClick={send} disabled={!text.trim() || sending} title="Send">
          ➤
        </button>
      </div>
      {error && <div className="error small">{error}</div>}
    </div>
  );
}
