import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getToken } from "../api";
import ModalShell from "./ModalShell";

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

type Tab = "live" | "screen" | "files";

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

export default function ComputerPanel({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const base = `/api/tasks/${taskId}/computer`;
  const [tab, setTab] = useState<Tab>("live");
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
        if (!stopped) timer = window.setTimeout(poll, 2500);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [base]);

  const run = async (label: string, work: () => Promise<void>) => {
    setBusy(label);
    try { await work(); setError(""); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(""); }
  };

  const openLive = (control: boolean) => run(control ? "control" : "live", async () => {
    setLive(await api<LiveView>("POST", `${base}/live`, { control }));
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

  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) || "/" : "";
  const control = live?.control ?? computer?.control ?? null;
  const viewOnly = !control?.mine;
  const liveSrc = live && `${live.url}?autoconnect=true&resize=scale&reconnect=true&view_only=${viewOnly}`;

  return (
    <ModalShell title="Agent computer" onClose={onClose} wide>
      <div className="computer-panel">
        <div className="computer-tabs" role="tablist">
          {(["live", "screen", "files"] as Tab[]).map((name) => (
            <button key={name} role="tab" aria-selected={tab === name} className={`chip ${tab === name ? "active" : ""}`}
              onClick={() => { setTab(name); if (name === "files" && files === null) void browse(""); }}>
              {name === "live" ? "Live" : name === "screen" ? "Latest screenshot" : "Files"}
            </button>
          ))}
          <button className="chip" disabled={busy === "stop"} onClick={() => void stop()}>Stop computer</button>
        </div>
        {error && <p role="alert">{error}</p>}
        {computer && !computer.configured && <p>Computer access is not configured for this workspace.</p>}
        {computer && (
          <p className="computer-status">Daytona · {computer.status.replaceAll("_", " ")}
            {computer.last_action && ` · ${computer.last_action}`}</p>
        )}
        {control && (
          <p className="computer-control">
            {control.mine ? "You control" : `${control.user_name} controls`} this computer until {time(control.until)}.
            The agent waits for its clicks, typing, and commands.
          </p>
        )}

        {tab === "live" && <>
          <div className="task-controls">
            <button className="chip" disabled={!!busy} onClick={() => void openLive(false)}>
              {live ? "Reconnect" : "Open live view"}
            </button>
            {!control?.mine && <button className="chip" disabled={!!busy} onClick={() => void openLive(true)}>Take over</button>}
            {control && <button className="chip" disabled={!!busy} onClick={() => void handBack()}>Hand back</button>}
          </div>
          {liveSrc ? <>
            <iframe key={liveSrc} className="computer-live" src={liveSrc} title="Live agent desktop"
              allow="clipboard-read; clipboard-write" referrerPolicy="no-referrer" />
            <p className="muted">Link valid until {time(live.expires_at)}; use Reconnect after that.
              {viewOnly ? " View only. Take over to use the mouse and keyboard." : ""}</p>
          </> : <p className="muted">Opening the live view starts this agent's computer if it is stopped.
            Anyone in this channel can open it, and view-only mode is a convenience rather than a security
            boundary. Taking over pauses the agent's actions for up to 15 minutes.</p>}
        </>}

        {tab === "screen" && computer && (computer.image ? <>
          <img className="computer-screen" src={computer.image} alt="Latest screenshot of the agent's Daytona desktop" />
          <p className="muted">Last observed screen · {computer.width} × {computer.height}
            {computer.updated_at && ` · ${new Date(computer.updated_at).toLocaleTimeString()}`}</p>
        </> : <p className="muted">Screenshots appear here after the agent uses its desktop.</p>)}

        {tab === "files" && <>
          <div className="task-controls">
            <button className="chip" disabled={!!busy || path === ""} onClick={() => void browse(parent)}>Up</button>
            <code>{path || "home"}</code>
            <label className="chip">
              Upload file
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
          <p className="muted">Uploads go to the agent's uploads folder and are announced in the channel. Up to 25 MB.</p>
        </>}

        {computer?.sandbox_id && (
          <p className="muted">Sandbox <code>{computer.sandbox_id}</code> ·{" "}
            <a href={computer.dashboard_url} target="_blank" rel="noreferrer">Daytona dashboard</a></p>
        )}
      </div>
    </ModalShell>
  );
}
