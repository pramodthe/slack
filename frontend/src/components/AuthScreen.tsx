import { useState, type FormEvent } from "react";
import { api } from "../api";

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body =
        mode === "signup" ? { username, display_name: displayName || username, password } : { username, password };
      const result = await api<{ token: string }>("POST", `/api/auth/${mode}`, body);
      onAuthed(result.token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-logo">◆</div>
        <h1>Band Slack</h1>
        <p className="muted">Chat with your team and a crew of AI agents, all in one place.</p>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        {mode === "signup" && (
          <label>
            Display name
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="e.g. Alice Chen" />
          </label>
        )}
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>
          {mode === "login" ? "Sign in" : "Create account"}
        </button>
        <button type="button" className="link" onClick={() => setMode(mode === "login" ? "signup" : "login")}>
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}
