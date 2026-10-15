const PALETTE = ["#e8912d", "#2bac76", "#1d9bd1", "#cd2553", "#7c3085", "#e01e5a", "#36c5f0", "#4a154b"];

const ROLE_ICONS: Record<string, string> = {
  master: "👑",
  coordinator: "🧭",
  researcher: "🔎",
  writer: "✍️",
  coder: "💻",
  critic: "🧐",
  custom: "🤖",
};

function colorFor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

export default function Avatar({
  name,
  kind,
  role,
  size = 36,
}: {
  name: string;
  kind: "human" | "agent" | "system" | "external";
  role?: string;
  size?: number;
}) {
  const style = { width: size, height: size, fontSize: size * 0.42, background: colorFor(name) };
  if (kind === "agent") {
    return (
      <div className="avatar avatar-agent" style={style} title={name}>
        {ROLE_ICONS[role ?? "custom"] ?? "🤖"}
      </div>
    );
  }
  if (kind === "system") {
    return (
      <div className="avatar avatar-system" style={{ ...style, background: "#616061" }}>
        !
      </div>
    );
  }
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div className="avatar" style={style} title={name}>
      {initials}
    </div>
  );
}
