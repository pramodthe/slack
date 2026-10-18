import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { SendHorizontalIcon } from "lucide-react";
import { api } from "../api";
import type { State } from "../store";
import type { Channel } from "../types";
import Avatar from "./Avatar";
import { Button } from "./ui/button";
import { Command, CommandItem, CommandList } from "./ui/command";
import { Popover, PopoverAnchor, PopoverContent } from "./ui/popover";
import { Textarea } from "./ui/textarea";

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
  const dmAgent = channel.dm_agent_id != null ? state.agents[channel.dm_agent_id] : null;

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
  const menuOpen = candidates.length > 0;

  const updateQuery = (value: string, caret: number) => {
    if (channel.is_dm) return;
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
    if (menuOpen) {
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
    <div className="tool-ui relative px-5 pb-[18px]">
      <Popover open={menuOpen} onOpenChange={(open) => !open && setQuery(null)}>
        <PopoverAnchor asChild>
          <div className="border-input flex items-end gap-2 rounded-lg border py-2 ps-3 pe-2 focus-within:border-[#888] focus-within:ring-1 focus-within:ring-[#888]">
            <Textarea
              ref={input}
              rows={1}
              value={text}
              role="combobox"
              aria-label="Message"
              aria-expanded={menuOpen}
              aria-autocomplete="list"
              className="max-h-[200px] min-h-6 flex-1 resize-none rounded-none border-0 p-0 py-0.5 text-[15px] shadow-none focus-visible:ring-0 md:text-[15px]"
              placeholder={
                dmAgent
                  ? `Message ${dmAgent.name}`
                  : master
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
            <Button
              size="icon-sm"
              className="h-[30px] w-[34px] rounded-md disabled:bg-[#ddd] disabled:opacity-100"
              onClick={send}
              disabled={!text.trim() || sending}
              title="Send"
              aria-label="Send"
            >
              <SendHorizontalIcon />
            </Button>
          </div>
        </PopoverAnchor>
        <PopoverContent
          side="top"
          align="start"
          sideOffset={6}
          className="w-[360px] p-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.15)]"
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          onMouseDown={(e) => e.preventDefault()}
        >
          <Command
            label="Mention someone"
            shouldFilter={false}
            value={candidates[highlight]?.key ?? ""}
            onValueChange={(key) => {
              const index = candidates.findIndex((c) => c.key === key);
              if (index >= 0) setHighlight(index);
            }}
          >
            <CommandList>
              {candidates.map((c) => (
                <CommandItem
                  key={c.key}
                  value={c.key}
                  onSelect={() => pick(c)}
                  className="group data-[selected=true]:bg-active cursor-pointer gap-2 rounded px-3 py-1.5 text-[15px] data-[selected=true]:text-white"
                >
                  <Avatar name={c.name} kind={c.kind} role={c.role} size={22} />
                  <span className="font-bold">{c.name}</span>
                  <span className="text-muted-foreground truncate group-data-[selected=true]:text-[#d6e6f3]">
                    {c.detail}
                  </span>
                  {!c.inChannel && (
                    <span className="text-muted-foreground ms-auto shrink-0 text-xs group-data-[selected=true]:text-[#d6e6f3]">
                      {c.kind === "agent" ? "will join" : "not in channel"}
                    </span>
                  )}
                </CommandItem>
              ))}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {error && <div className="text-destructive mt-1 text-[12.5px]">{error}</div>}
    </div>
  );
}
