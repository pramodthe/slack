import { useEffect, useState } from "react";
import { api } from "../api";
import ModalShell from "./ModalShell";

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
}

export default function ComputerPanel({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [computer, setComputer] = useState<ComputerState | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await api<ComputerState>("GET", `/api/tasks/${taskId}/computer`);
        if (!stopped) { setComputer(next); setError(""); }
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 2500);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [taskId]);

  return (
    <ModalShell title="Task computer" onClose={onClose} wide>
    <div className="computer-panel">
      {error && <p role="alert">{error}</p>}
      {!computer && !error && <p className="muted">Loading computer…</p>}
      {computer && <>
        {!computer.configured && <p>Computer access is not configured for this workspace.</p>}
        <p className="computer-status">Daytona · {computer.status.replaceAll("_", " ")}
          {computer.last_action && ` · ${computer.last_action}`}</p>
        {computer.error && <p role="alert">{computer.error}</p>}
        {computer.image ? <>
          <img className="computer-screen" src={computer.image} alt="Latest screenshot of the agent's Daytona desktop" />
          <p className="muted">Last observed screen · {computer.width} × {computer.height}
            {computer.updated_at && ` · ${new Date(computer.updated_at).toLocaleTimeString()}`}</p>
        </> : computer.configured && <p className="muted">The agent starts its computer when it needs desktop or code tools. Screenshots appear here after desktop actions.</p>}
        {computer.sandbox_id && <>
          <p className="muted">Sandbox: <code>{computer.sandbox_id}</code></p>
          <a className="chip" href={computer.dashboard_url} target="_blank" rel="noreferrer">Open Daytona dashboard</a>
          <p className="muted">Choose this sandbox’s VNC option to watch its live desktop. This panel shows the agent’s latest observation.</p>
        </>}
      </>}
    </div>
    </ModalShell>
  );
}
