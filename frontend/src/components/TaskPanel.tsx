import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import ComputerPanel from "./ComputerPanel";
import type { State, Action } from "../store";
import type { Message } from "../types";
import type { Dispatch } from "react";

interface Task {
  id: string;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled";
  progress: string;
  error: string | null;
  cancel_requested: boolean;
  delivery_status: string;
  updated_at: string;
}
interface Memory { key: string; content: string; updated_at: string; }
interface Approval { id: string; task_id: string; agent_id: number; action: string; details: string; }
interface Schedule { id: number; agent_id: number; prompt: string; when: string; next_run_at: string | null; }

interface TaskDetail extends Task {
  result: string;
  artifacts: { path: string; content: string }[];
}

export default function TaskPanel({ channelId, state, dispatch }: {
  channelId: number; state: State; dispatch: Dispatch<Action>;
}) {
  const [expanded, setExpanded] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [computerTask, setComputerTask] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [memory, setMemory] = useState<{ agentId: number; rows: Memory[] } | null>(null);
  const version = useRef("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    version.current = "";
    setTasks([]);
    setApprovals([]);
    setSchedules([]);
    setDetail(null);
    setComputerTask(null);
    setMemory(null);
    const poll = async () => {
      try {
        const [rows, pending, planned] = await Promise.all([
          api<Task[]>("GET", `/api/channels/${channelId}/tasks`),
          api<Approval[]>("GET", `/api/channels/${channelId}/approvals`),
          api<Schedule[]>("GET", `/api/channels/${channelId}/schedules`),
        ]);
        if (stopped) return;
        setTasks(rows);
        setApprovals(pending);
        setSchedules(planned);
        dispatch({ type: "tasks_loaded", channelId, tasks: rows.map((t) => ({
          id: t.id, channel_id: channelId, agent_id: t.agent_id,
          status: t.status === "queued" || t.status === "running" || t.status === "waiting" ? t.status : "done",
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

  const decide = async (approval: Approval, approve: boolean) => {
    setBusy(approval.id);
    try {
      await api("POST", `/api/approvals/${approval.id}`, { approve });
      setApprovals((rows) => rows.filter((a) => a.id !== approval.id));
      setTasks((rows) => rows.map((t) => t.id === approval.task_id ? { ...t, progress: "Decision received; resuming" } : t));
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

  const inspect = async (id: string) => {
    try { setDetail(await api<TaskDetail>("GET", `/api/tasks/${id}`)); }
    catch (err) { setError((err as Error).message); }
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

  const download = (artifact: { path: string; content: string }) => {
    const url = URL.createObjectURL(new Blob([artifact.content], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = artifact.path.split("/").pop() || "artifact.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <details className="task-panel" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>
        Agent tasks · {tasks.filter((t) => t.status === "running" || t.status === "queued").length} active
        {approvals.length > 0 && ` · ${approvals.length} awaiting approval`}
        {schedules.length > 0 && ` · ${schedules.length} scheduled`}
      </summary>
      <p className="muted">Tasks continue while you are away. Queued tasks wait for the background worker.</p>
      {error && <p role="alert">{error}</p>}
      {tasks.length === 0 && <p className="muted">Mention an agent to start a task.</p>}
      {approvals.map((approval) => (
        <div className="approval-card" key={approval.id}>
          <div>
            <strong>{state.agents[approval.agent_id]?.name ?? "Agent"} needs approval</strong>
            <p>{approval.action}</p>
            {approval.details && <p className="muted">{approval.details}</p>}
          </div>
          <div className="task-controls">
            <button className="chip" disabled={busy === approval.id} onClick={() => void decide(approval, true)}>Approve</button>
            <button className="chip" disabled={busy === approval.id} onClick={() => void decide(approval, false)}>Deny</button>
          </div>
        </div>
      ))}
      {schedules.map((schedule) => (
        <div className="task-row" key={`schedule-${schedule.id}`}>
          <div>
            <strong>{state.agents[schedule.agent_id]?.name ?? "Agent"}</strong>
            <span className="task-status">{schedule.when}</span>
            <p>{schedule.prompt}</p>
            {schedule.next_run_at && <p className="muted">Next run {new Date(schedule.next_run_at).toLocaleString()}</p>}
          </div>
          <div className="task-controls">
            <button className="chip" onClick={() => void cancelSchedule(schedule)}>Cancel schedule</button>
          </div>
        </div>
      ))}
      {tasks.map((task) => (
        <div className="task-row" key={task.id}>
          <div>
            <strong>{state.agents[task.agent_id]?.name ?? "Agent"}</strong>
            <span className={`task-status task-${task.status}`}>{task.status}</span>
            <p>{task.cancel_requested && task.status === "running" ? "Cancelling…" : task.progress}</p>
            {task.error && <p role="alert">{task.error}</p>}
            {task.delivery_status === "unknown" && <p>Answer saved here. Band delivery could not be confirmed.</p>}
          </div>
          <div className="task-controls">
            <button className="chip" onClick={() => setComputerTask(task.id)}>Computer</button>
            <button className="chip" onClick={() => void showMemory(task.agent_id)}>Memory</button>
            {(task.status === "queued" || task.status === "running" || task.status === "waiting") && (
              <button className="chip" disabled={busy === task.id || task.cancel_requested} onClick={() => void control(task.id, "cancel")}>Cancel</button>
            )}
            {(task.status === "failed" || task.status === "cancelled") && (
              <button className="chip" disabled={busy === task.id} onClick={() => void control(task.id, "retry")}>Resume</button>
            )}
            {task.status === "completed" && <button className="chip" onClick={() => void inspect(task.id)}>Result & files</button>}
          </div>
        </div>
      ))}
      {computerTask && <ComputerPanel key={computerTask} taskId={computerTask} onClose={() => setComputerTask(null)} />}
      {memory && (
        <div className="task-result">
          <button className="chip" onClick={() => setMemory(null)}>Close memory</button>
          <p>Saved knowledge for {state.agents[memory.agentId]?.name ?? "this agent"} in this channel.</p>
          {memory.rows.length === 0 && <p className="muted">No saved memories yet.</p>}
          {memory.rows.map((item) => <div className="task-row" key={item.key}>
            <div><strong>{item.key}</strong><p>{item.content}</p></div>
            <button className="chip" onClick={() => void forget(memory.agentId, item.key)}>Forget</button>
          </div>)}
        </div>
      )}
      {detail && (
        <div className="task-result">
          <button className="chip" onClick={() => setDetail(null)}>Close result</button>
          <pre>{detail.result}</pre>
          {detail.artifacts.map((artifact) => <button className="chip" key={artifact.path} onClick={() => download(artifact)}>Download {artifact.path}</button>)}
        </div>
      )}
    </details>
  );
}
