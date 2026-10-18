import { useEffect, useRef, useState, type Dispatch } from "react";
import { ChevronRightIcon, XIcon } from "lucide-react";
import { api } from "../api";
import type { State, Action } from "../store";
import type { Artifact, Memory, Message, Schedule, Task, TaskDetail } from "../types";
import type { OpenComputer } from "./Workspace";
import RichText from "./RichText";
import { Button } from "@/components/ui/button";
import { cn, parseUtc } from "@/lib/utils";
import { ArtifactCard } from "./assistant-ui/elements/artifact-card";
import { MemoryChips } from "./assistant-ui/elements/memory-chips";
import { ScheduleCard } from "./assistant-ui/elements/schedule-card";
import { TaskCard, type TaskCardState } from "./assistant-ui/elements/task-card";
import { ToolError } from "./assistant-ui/elements/tool-error";

const CARD_STATE: Record<Task["status"], TaskCardState> = {
  queued: "waiting",
  running: "working",
  waiting: "waiting",
  completed: "done",
  failed: "failed",
  cancelled: "cancelled",
};

const isActive = (t: Task) => t.status === "queued" || t.status === "running" || t.status === "waiting";

function since(iso: string) {
  const seconds = Math.max(0, (Date.now() - parseUtc(iso).getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return parseUtc(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

const words = (text: string) => text.split(/\s+/).filter(Boolean).length;

function download(artifact: Artifact) {
  const url = URL.createObjectURL(new Blob([artifact.content], { type: "text/plain" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = artifact.path.split("/").pop() || "artifact.txt";
  link.click();
  URL.revokeObjectURL(url);
}

function TaskResult({ detail }: { detail: TaskDetail }) {
  return (
    <>
      <div className="max-h-64 overflow-y-auto text-[13px]">
        <RichText text={detail.result} />
      </div>
      {detail.artifacts.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {detail.artifacts.map((artifact) => (
            <ArtifactCard
              key={artifact.path}
              role="button"
              tabIndex={0}
              aria-label={`Download ${artifact.path}`}
              title={artifact.path.split("/").pop() || artifact.path}
              meta={`${artifact.path} · ${words(artifact.content)} words`}
              onClick={() => download(artifact)}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && download(artifact)}
            />
          ))}
        </div>
      )}
    </>
  );
}

export default function TaskPanel({ channelId, state, dispatch, openComputer }: {
  channelId: number; state: State; dispatch: Dispatch<Action>; openComputer: OpenComputer;
}) {
  const [expanded, setExpanded] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TaskDetail>>({});
  const [memory, setMemory] = useState<{ agentId: number; rows: Memory[] } | null>(null);
  const version = useRef("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    version.current = "";
    setTasks([]);
    setSchedules([]);
    setResults({});
    setMemory(null);
    const poll = async () => {
      try {
        const [rows, planned] = await Promise.all([
          api<Task[]>("GET", `/api/channels/${channelId}/tasks`),
          api<Schedule[]>("GET", `/api/channels/${channelId}/schedules`),
        ]);
        if (stopped) return;
        setTasks(rows);
        setSchedules(planned);
        dispatch({ type: "tasks_loaded", channelId, tasks: rows.map((t) => ({
          id: t.id, channel_id: channelId, agent_id: t.agent_id,
          status: isActive(t) ? t.status as "queued" | "running" | "waiting" : "done",
          progress: t.progress,
        })) });
        setError("");
        const next = rows.filter((t) => t.status === "completed").map((t) => t.id).join(",");
        if (next !== version.current) {
          const messages = await api<Message[]>("GET", `/api/channels/${channelId}/messages`);
          if (stopped) return;
          dispatch({ type: "messages_loaded", channelId, messages });
          version.current = next;
        }
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId, dispatch]);

  const control = async (id: string, action: "cancel" | "retry") => {
    setBusy(id);
    try {
      const updated = await api<Task>("POST", `/api/tasks/${id}/${action}`);
      setTasks((rows) => rows.map((t) => t.id === id ? updated : t));
      setError("");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(null); }
  };

  const cancelSchedule = async (schedule: Schedule) => {
    try {
      await api("DELETE", `/api/channels/${channelId}/schedules/${schedule.id}`);
      setSchedules((rows) => rows.filter((s) => s.id !== schedule.id));
      setError("");
    } catch (err) { setError((err as Error).message); }
  };

  const loadResult = async (id: string) => {
    if (results[id]) return;
    try {
      const detail = await api<TaskDetail>("GET", `/api/tasks/${id}`);
      setResults((current) => ({ ...current, [id]: detail }));
    } catch (err) { setError((err as Error).message); }
  };

  const showMemory = async (agentId: number) => {
    try {
      const rows = await api<Memory[]>("GET", `/api/channels/${channelId}/agents/${agentId}/memories`);
      setMemory({ agentId, rows });
    } catch (err) { setError((err as Error).message); }
  };

  const forget = async (agentId: number, key: string) => {
    try {
      await api("DELETE", `/api/channels/${channelId}/agents/${agentId}/memories/${encodeURIComponent(key)}`);
      await showMemory(agentId);
    } catch (err) { setError((err as Error).message); }
  };

  const agentName = (id: number) => state.agents[id]?.name ?? "Agent";
  const active = tasks.filter((t) => t.status === "running" || t.status === "queued").length;
  const waiting = tasks.filter((t) => t.status === "waiting").length;

  return (
    <section className="tool-ui mx-6 mb-2 flex max-h-[32vh] flex-col overflow-hidden rounded-lg border border-border bg-background text-[13px]">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 px-3.5 py-2 text-start font-bold hover:bg-muted"
      >
        <ChevronRightIcon className={cn("size-3.5 transition-transform", expanded && "rotate-90")} />
        Agent tasks · {active} active
        {waiting > 0 && ` · ${waiting} awaiting approval`}
        {schedules.length > 0 && ` · ${schedules.length} scheduled`}
      </button>
      {expanded && (
        <div className="flex min-h-0 flex-col gap-2 overflow-y-auto border-t border-border px-3.5 py-2.5">
          <p className="text-muted-foreground">Tasks continue while you are away. Queued tasks wait for the background worker.</p>
          {error && <p role="alert" className="text-destructive">{error}</p>}
          {tasks.length === 0 && schedules.length === 0 && (
            <p className="text-muted-foreground">Mention an agent to start a task.</p>
          )}
          {memory && (
            <div className="border-border flex flex-col gap-2 rounded-xl border p-3">
              <div className="flex items-center gap-2">
                <span className="flex-1">
                  Saved knowledge for <strong>{agentName(memory.agentId)}</strong> in this channel.
                </span>
                <Button variant="ghost" size="icon-xs" aria-label="Close memory" onClick={() => setMemory(null)}>
                  <XIcon />
                </Button>
              </div>
              {memory.rows.length === 0 ? (
                <p className="text-muted-foreground">No saved memories yet.</p>
              ) : (
                <MemoryChips
                  className="max-w-none"
                  chips={memory.rows.map((row) => {
                    const text = `${row.key}: ${row.content}`;
                    return { id: row.key, text: text.length > 90 ? `${text.slice(0, 89)}…` : text, change: "existing" };
                  })}
                  onForget={(key) => void forget(memory.agentId, key)}
                />
              )}
            </div>
          )}
          <div className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] items-start gap-2">
            {schedules.map((schedule) => (
              <ScheduleCard
                key={`schedule-${schedule.id}`}
                className="max-w-none"
                name={`${agentName(schedule.agent_id)}: ${schedule.prompt}`}
                cadence={schedule.when}
                nextRun={schedule.next_run_at
                  ? parseUtc(schedule.next_run_at).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
                  : "not scheduled"}
                enabled
                history={[]}
                action={
                  <Button variant="chip" size="chip" onClick={() => void cancelSchedule(schedule)}>
                    Cancel
                  </Button>
                }
              />
            ))}
            {tasks.map((task) =>
              task.status === "failed" ? (
                <ToolError
                  key={task.id}
                  className="max-w-none"
                  name={agentName(task.agent_id)}
                  target={task.progress}
                  message={task.error ?? "The task failed."}
                  attempt={task.attempts}
                  retrying={busy === task.id}
                  onRetry={() => void control(task.id, "retry")}
                />
              ) : (
                <TaskCard
                  key={task.id}
                  className="max-w-none"
                  state={CARD_STATE[task.status]}
                  label={task.cancel_requested && task.status === "running" ? "Cancelling…" : task.progress}
                  meta={agentName(task.agent_id)}
                  elapsed={since(task.updated_at)}
                  onOpenChange={(open) => open && void loadResult(task.id)}
                  actions={
                    <div className="flex flex-wrap gap-1.5">
                      <Button variant="chip" size="chip" onClick={() => openComputer(task.agent_id, task.id)}>
                        Computer
                      </Button>
                      <Button variant="chip" size="chip" onClick={() => void showMemory(task.agent_id)}>
                        Memory
                      </Button>
                      {isActive(task) && (
                        <Button
                          variant="chip"
                          size="chip"
                          disabled={busy === task.id || task.cancel_requested}
                          onClick={() => void control(task.id, "cancel")}
                        >
                          Cancel
                        </Button>
                      )}
                      {task.status === "cancelled" && (
                        <Button variant="chip" size="chip" disabled={busy === task.id} onClick={() => void control(task.id, "retry")}>
                          Resume
                        </Button>
                      )}
                    </div>
                  }
                  result={task.delivery_status === "unknown" ? "Answer saved here. Band delivery could not be confirmed." : undefined}
                >
                  {task.status === "completed" &&
                    (results[task.id] ? <TaskResult detail={results[task.id]} /> : <p className="text-muted-foreground">Loading result…</p>)}
                </TaskCard>
              ),
            )}
          </div>
        </div>
      )}
    </section>
  );
}
