import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getToken } from "../api";
import type { Agent } from "../types";
import Avatar from "./Avatar";

interface Control {
  user_name: string;
  until: string;
  mine: boolean;
}

interface ComputerState {
  configured: boolean;
  status: string;
  sandbox_id: string | null;
  image: string;
  width: number;
  height: number;
  last_action: string;
  updated_at: string | null;
  error: string | null;
  dashboard_url: string;
  control: Control | null;
}

interface LiveView {
  url: string;
  expires_at: string;
  control: Control | null;
}

interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

type Tab = "screen" | "live" | "files";

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function raw(method: string, path: string, body?: Blob): Promise<Response> {
  const token = getToken();
  const response = await fetch(path, { method, body, headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(typeof data.detail === "string" ? data.detail : "Request failed", response.status);
  }
  return response;
}

export default function ComputerPanel({ taskId, agent, working, others, onSwitch, onClose }: {
  taskId: string;
  agent: Agent | undefined;
  working: boolean;
  others: { agent: Agent; working: boolean }[];
  onSwitch: (agentId: number) => void;
  onClose: () => void;
}) {
  const base = `/api/tasks/${taskId}/computer`;
  const name = agent?.name ?? "Agent";
  const [tab, setTab] = useState<Tab>("screen");
  const [computer, setComputer] = useState<ComputerState | null>(null);
  const [live, setLive] = useState<LiveView | null>(null);
  const [files, setFiles] = useState<FileEntry[] | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await api<ComputerState>("GET", base);
        if (!stopped) setComputer(next);
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, working ? 1500 : 4000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [base, working]);

  const run = async (label: string, work: () => Promise<void>) => {
    setBusy(label);
    try { await work(); setError(""); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(""); }
  };

  const openLive = (control: boolean) => run(control ? "control" : "live", async () => {
    setLive(await api<LiveView>("POST", `${base}/live`, { control }));
    setTab("live");
  });

  const handBack = () => run("release", async () => {
    await api("DELETE", `${base}/control`);
    setLive((view) => view && { ...view, control: null });
  });

  const stop = () => run("stop", async () => {
    await api("POST", `${base}/stop`);
    setLive(null);
  });

  const browse = useCallback((next: string) => run("files", async () => {
    setFiles(await api<FileEntry[]>("GET", `${base}/files?path=${encodeURIComponent(next)}`));
    setPath(next);
  }), [base]);

  const download = (entry: FileEntry) => run(`download-${entry.path}`, async () => {
    const response = await raw("GET", `${base}/files/download?path=${encodeURIComponent(entry.path)}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = entry.name;
    link.click();
    URL.revokeObjectURL(url);
  });

  const upload = (file: File) => run("upload", async () => {
    await raw("POST", `${base}/files?name=${encodeURIComponent(file.name)}`, file);
    await browse(path);
  });

  const selectTab = (next: Tab) => {
    setTab(next);
    if (next === "files" && files === null) void browse("");
    // Connecting to a computer that is already up costs nothing; a stopped one waits for an explicit click.
    if (next === "live" && !live && computer?.status === "running") void openLive(false);
  };

  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) || "/" : "";
  const control = live?.control ?? computer?.control ?? null;
  const viewOnly = !control?.mine;
  const liveSrc = live && `${live.url}?autoconnect=true&resize=scale&reconnect=true&view_only=${viewOnly}`;
  const status = computer?.status.replaceAll("_", " ") ?? "loading";
  const dot = computer?.status === "running" ? "online" : computer?.error ? "error" : "stopped";

  return (
    <aside className="computer-sidebar" aria-label={`${name}'s computer`}>
      <header className="computer-head">
        <Avatar name={name} kind="agent" role={agent?.role} size={28} />
        <div className="computer-head-text">
          <div className="computer-title">{name}'s computer</div>
          <div className="computer-sub">
            <span className={`dot dot-${dot}`} /> <span className="computer-status">{status}</span>
            {working && <span className="live-pill">● Working</span>}
          </div>
        </div>
        <button className="icon-btn subtle" title="Close computer" aria-label="Close computer" onClick={onClose}>×</button>
      </header>

      {others.length > 0 && (
        <div className="computer-agents">
          <span className="muted small">Also in this channel:</span>
          {others.map((o) => (
            <button key={o.agent.id} className="chip" onClick={() => onSwitch(o.agent.id)}>
              {o.agent.name}{o.working ? " ●" : ""}
            </button>
          ))}
        </div>
      )}

      <div className="computer-tabs" role="tablist">
        {(["screen", "live", "files"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={`chip ${tab === t ? "active" : ""}`}
            onClick={() => selectTab(t)}>
            {t === "screen" ? "Screen" : t === "live" ? "Live desktop" : "Files"}
          </button>
        ))}
      </div>

      <div className="computer-body">
        {error && <p className="error small" role="alert">{error}</p>}
        {computer && !computer.configured && <p className="muted">Computer access is not configured for this workspace.</p>}
        {computer?.error && <p className="error small">{computer.error}</p>}
        {control && (
          <p className="computer-control">
            {control.mine ? "You control" : `${control.user_name} controls`} this computer until {time(control.until)}.
            The agent waits for its clicks, typing, and commands.
          </p>
        )}

        {tab === "screen" && (computer?.image ? <>
          <div className="computer-frame">
            <img className="computer-screen" src={computer.image} alt={`${name}'s screen`} />
            {computer.last_action && <div className="computer-caption">{computer.last_action}</div>}
          </div>
          <p className="muted small">
            {working ? "Updates as the agent works" : "Last screen the agent saw"}
            {computer.updated_at && ` · ${new Date(computer.updated_at).toLocaleTimeString()}`}
          </p>
        </> : (
          <div className="computer-empty">
            <div className="computer-empty-icon">🖥</div>
            <p>No screen yet. It appears here as soon as {name} uses its desktop.</p>
            {computer?.last_action && <p className="muted small">Last action: {computer.last_action}</p>}
            <p className="muted small">Commands and files don't produce screens; see the Files tab or connect live.</p>
          </div>
        ))}

        {tab === "live" && <>
          {liveSrc ? <>
            <iframe key={liveSrc} className="computer-live" src={liveSrc} title={`${name}'s live desktop`}
              allow="clipboard-read; clipboard-write" referrerPolicy="no-referrer" />
            <p className="muted small">Link valid until {time(live.expires_at)}.
              {viewOnly ? " View only: take over to use the mouse and keyboard." : ""}</p>
          </> : (
            <div className="computer-empty">
              <p>Watch {name}'s desktop in real time, or take over the mouse and keyboard.</p>
              <p className="muted small">Connecting starts the computer if it is stopped. Taking over pauses the
                agent's actions for up to 15 minutes. View-only mode is a convenience, not a security boundary.</p>
            </div>
          )}
          <div className="task-controls">
            <button className="chip" disabled={!!busy} onClick={() => void openLive(false)}>
              {live ? "Reconnect" : "Connect"}
            </button>
            {!control?.mine && <button className="chip" disabled={!!busy} onClick={() => void openLive(true)}>Take over</button>}
            {control && <button className="chip" disabled={!!busy} onClick={() => void handBack()}>Hand back</button>}
          </div>
        </>}

        {tab === "files" && <>
          <div className="task-controls">
            <button className="chip" disabled={!!busy || path === ""} onClick={() => void browse(parent)}>Up</button>
            <code>{path || "home"}</code>
            <label className="chip">
              Upload
              <input type="file" hidden disabled={!!busy}
                onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file); }} />
            </label>
          </div>
          {busy === "files" && <p className="muted">Loading files…</p>}
          {files?.length === 0 && <p className="muted">This folder is empty.</p>}
          {files?.map((entry) => (
            <div className="task-row" key={entry.path}>
              <div>
                {entry.is_dir
                  ? <button className="link" onClick={() => void browse(entry.path)}>📁 {entry.name}</button>
                  : <span>{entry.name}</span>}
                {!entry.is_dir && <span className="muted"> · {formatSize(entry.size)}</span>}
              </div>
              {!entry.is_dir && (
                <button className="chip" disabled={!!busy} onClick={() => void download(entry)}>Download</button>
              )}
            </div>
          ))}
          <p className="muted small">Uploads go to the agent's uploads folder and are announced in the channel. Up to 25 MB.</p>
        </>}
      </div>

      <footer className="computer-foot">
        <button className="chip" disabled={busy === "stop"} onClick={() => void stop()}>Stop computer</button>
        {computer?.sandbox_id && (
          <a className="small" href={computer.dashboard_url} target="_blank" rel="noreferrer">Daytona dashboard</a>
        )}
      </footer>
    </aside>
  );
}
