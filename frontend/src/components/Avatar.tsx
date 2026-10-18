import { cn } from "@/lib/utils";
import { Avatar as AvatarRoot, AvatarFallback } from "./ui/avatar";

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

function initials(name: string) {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
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
  const label =
    kind === "agent" ? (ROLE_ICONS[role ?? "custom"] ?? "🤖") : kind === "system" ? "!" : initials(name);
  return (
    <AvatarRoot
      className={cn("rounded-md", kind === "agent" && "rounded-[10px]")}
      style={{ width: size, height: size, fontSize: size * 0.42 }}
      title={kind === "system" ? undefined : name}
    >
      <AvatarFallback
        className="rounded-[inherit] text-[length:inherit] leading-none font-black text-white"
        style={{ background: kind === "system" ? "#616061" : colorFor(name) }}
      >
        {label}
      </AvatarFallback>
    </AvatarRoot>
  );
}
