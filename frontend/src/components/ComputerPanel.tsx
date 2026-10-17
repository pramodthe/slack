import { useCallback, useEffect, useState } from "react";
import { FolderIcon, MonitorIcon, XIcon } from "lucide-react";
import { api, ApiError, getToken } from "../api";
import type { Agent, ComputerState, FileEntry, LiveView } from "../types";
import { cn, parseUtc } from "@/lib/utils";
import Avatar from "./Avatar";
import { ComputerUse, type ComputerStep } from "./assistant-ui/elements/computer-use";
import StatusDot from "./StatusDot";
import { Button } from "./ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";

type Tab = "screen" | "live" | "files";

const MAX_STEPS = 20;

const time = (iso: string) => parseUtc(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const clock = (iso: string) => parseUtc(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

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

const note = "text-muted-foreground text-xs";
const emptyBox = "flex flex-col items-center gap-1.5 rounded-lg border border-dashed border-border bg-background px-4 py-9 text-center";

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
  const [steps, setSteps] = useState<ComputerStep[]>([]);
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

  // The server keeps only the latest action, so the trail is what this panel has seen while open.
  const lastAction = computer?.last_action ?? "";
  const updatedAt = computer?.updated_at ?? null;
  useEffect(() => {
    if (!lastAction || !updatedAt) return;
    setSteps((prev) => prev.at(-1)?.id === updatedAt
      ? prev
      : [...prev, { id: updatedAt, action: clock(updatedAt), target: lastAction }].slice(-MAX_STEPS));
  }, [lastAction, updatedAt]);

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
    <aside
      className="tool-ui border-border flex min-h-0 w-[min(560px,44vw)] shrink-0 flex-col border-l bg-[#fafafa] text-[13px] narrow:fixed narrow:inset-y-0 narrow:right-0 narrow:z-20 narrow:w-[min(100vw,560px)] narrow:shadow-[-4px_0_16px_rgba(0,0,0,0.2)]"
      aria-label={`${name}'s computer`}
    >
      <header className="border-border bg-background flex min-h-[58px] items-center gap-2.5 border-b px-4 py-3">
        <Avatar name={name} kind="agent" role={agent?.role} size={28} />
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-black">{name}'s computer</div>
          <div className="text-muted-foreground flex items-center gap-1.5 text-xs">
            <StatusDot state={dot} />
            <span className="capitalize">{status}</span>
            {working && <span className="animate-pulse font-bold text-[#1d9b5f]">● Working</span>}
          </div>
        </div>
        <Button variant="ghost" size="icon-xs" title="Close computer" aria-label="Close computer" onClick={onClose}>
          <XIcon />
        </Button>
      </header>

      {others.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-4 pt-2">
          <span className={note}>Also in this channel:</span>
          {others.map((o) => (
            <Button key={o.agent.id} variant="chip" size="chip" onClick={() => onSwitch(o.agent.id)}>
              {o.agent.name}{o.working ? " ●" : ""}
            </Button>
          ))}
        </div>
      )}

      <Tabs value={tab} onValueChange={(next) => selectTab(next as Tab)} className="min-h-0 flex-1 gap-0">
        <TabsList className="mx-4 mt-2.5">
          <TabsTrigger value="screen">Screen</TabsTrigger>
          <TabsTrigger value="live">Live desktop</TabsTrigger>
          <TabsTrigger value="files">Files</TabsTrigger>
        </TabsList>

        <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-2.5">
          {error && <p className="text-destructive text-xs" role="alert">{error}</p>}
          {computer && !computer.configured && <p className="text-muted-foreground">Computer access is not configured for this workspace.</p>}
          {computer?.error && <p className="text-destructive text-xs">{computer.error}</p>}
          {control && (
            <p className="rounded-md border border-[#d9a400] bg-[#ffc400]/12 px-2.5 py-1.5">
              {control.mine ? "You control" : `${control.user_name} controls`} this computer until {time(control.until)}.
              The agent waits for its clicks, typing, and commands.
            </p>
          )}

          <TabsContent value="screen" className="flex flex-col gap-2">
            {computer?.image ? <>
              <ComputerUse className="max-w-none" url={`${name}'s desktop`} steps={steps} activeIndex={steps.length - 1}>
                <img className="block h-auto w-full bg-[#181818]" src={computer.image} alt={`${name}'s screen`} />
              </ComputerUse>
              <p className={note}>
                {working ? "Updates as the agent works" : "Last screen the agent saw"}
                {computer.updated_at && ` · ${clock(computer.updated_at)}`}
              </p>
            </> : (
              <div className={emptyBox}>
                <MonitorIcon className="text-muted-foreground size-8 opacity-60" />
                <p>No screen yet. It appears here as soon as {name} uses its desktop.</p>
                {computer?.last_action && <p className={note}>Last action: {computer.last_action}</p>}
                <p className={note}>Commands and files don't produce screens; see the Files tab or connect live.</p>
              </div>
            )}
          </TabsContent>

          <TabsContent value="live" className="flex flex-col gap-2">
            {liveSrc ? <>
              <iframe key={liveSrc} className="block aspect-[16/10] w-full rounded-lg bg-[#181818]" src={liveSrc}
                title={`${name}'s live desktop`} allow="clipboard-read; clipboard-write" referrerPolicy="no-referrer" />
              <p className={note}>Link valid until {time(live.expires_at)}.
                {viewOnly ? " View only: take over to use the mouse and keyboard." : ""}</p>
            </> : (
              <div className={emptyBox}>
                <p>Watch {name}'s desktop in real time, or take over the mouse and keyboard.</p>
                <p className={note}>Connecting starts the computer if it is stopped. Taking over pauses the
                  agent's actions for up to 15 minutes. View-only mode is a convenience, not a security boundary.</p>
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button variant="chip" size="chip" disabled={!!busy} onClick={() => void openLive(false)}>
                {live ? "Reconnect" : "Connect"}
              </Button>
              {!control?.mine && (
                <Button variant="chip" size="chip" disabled={!!busy} onClick={() => void openLive(true)}>Take over</Button>
              )}
              {control && <Button variant="chip" size="chip" disabled={!!busy} onClick={() => void handBack()}>Hand back</Button>}
            </div>
          </TabsContent>

          <TabsContent value="files" className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Button variant="chip" size="chip" disabled={!!busy || path === ""} onClick={() => void browse(parent)}>Up</Button>
              <code className="min-w-0 flex-1 truncate">{path || "home"}</code>
              <Button asChild variant="chip" size="chip" className={cn("cursor-pointer", busy && "pointer-events-none opacity-50")}>
                <label>
                  Upload
                  <input type="file" hidden disabled={!!busy}
                    onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file); }} />
                </label>
              </Button>
            </div>
            {busy === "files" && <p className="text-muted-foreground">Loading files…</p>}
            {files?.length === 0 && <p className="text-muted-foreground">This folder is empty.</p>}
            {files && files.length > 0 && (
              <ul className="border-border bg-background divide-border divide-y rounded-lg border">
                {files.map((entry) => (
                  <li key={entry.path} className="flex min-h-10 items-center gap-2 px-3 py-1.5">
                    {entry.is_dir ? (
                      <Button variant="link" size="inline" className="min-w-0 justify-start text-[13px]" onClick={() => void browse(entry.path)}>
                        <FolderIcon className="text-muted-foreground" />
                        <span className="truncate">{entry.name}</span>
                      </Button>
                    ) : (
                      <span className="min-w-0 truncate">{entry.name}</span>
                    )}
                    {!entry.is_dir && <span className="text-muted-foreground shrink-0">{formatSize(entry.size)}</span>}
                    {!entry.is_dir && (
                      <Button variant="chip" size="chip" className="ms-auto" disabled={!!busy} onClick={() => void download(entry)}>
                        Download
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className={note}>Uploads go to the agent's uploads folder and are announced in the channel. Up to 25 MB.</p>
          </TabsContent>
        </div>
      </Tabs>

      <footer className="border-border bg-background flex items-center justify-between gap-2 border-t px-4 py-2.5">
        <Button variant="chip" size="chip" disabled={busy === "stop"} onClick={() => void stop()}>Stop computer</Button>
        {computer?.sandbox_id && (
          <a className="text-mention text-xs hover:underline" href={computer.dashboard_url} target="_blank" rel="noreferrer">
            Daytona dashboard
          </a>
        )}
      </footer>
    </aside>
  );
}
