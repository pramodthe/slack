import { useId, useState, type FormEvent } from "react";
import { api } from "../api";
import Field from "./Field";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const id = useId();
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
    <div className="tool-ui grid min-h-full place-items-center bg-[linear-gradient(135deg,#3f0e40,#611f69)]">
      <form
        className="bg-background flex w-[380px] max-w-[calc(100vw-32px)] flex-col gap-3.5 rounded-xl p-8 shadow-[0_20px_60px_rgba(0,0,0,0.3)]"
        onSubmit={submit}
      >
        <div className="text-[32px] leading-none text-[#611f69]">◆</div>
        <h1 className="text-[2em] font-bold">Band Slack</h1>
        <p className="text-muted-foreground mb-1.5">Chat with your team and a crew of AI agents, all in one place.</p>
        <Field id={`${id}-username`} label="Username">
          <Input id={`${id}-username`} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </Field>
        {mode === "signup" && (
          <Field id={`${id}-display`} label="Display name">
            <Input
              id={`${id}-display`}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. Alice Chen"
            />
          </Field>
        )}
        <Field id={`${id}-password`} label="Password">
          <Input id={`${id}-password`} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        {error && <div className="text-destructive text-[13.5px]">{error}</div>}
        <Button disabled={busy}>{mode === "login" ? "Sign in" : "Create account"}</Button>
        <Button type="button" variant="link" size="inline" onClick={() => setMode(mode === "login" ? "signup" : "login")}>
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </Button>
      </form>
    </div>
  );
}
