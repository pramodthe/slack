import { createContext, useContext, useMemo, type ReactNode } from "react";

const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function inlinePattern(names: string[]): RegExp {
  const known = [...names]
    .filter((n) => /\s/.test(n))
    .sort((a, b) => b.length - a.length)
    .map(escapeRegex);
  const mention = known.length ? `@(?:${known.join("|")})|@[\\w.\\-/]+` : "@[\\w.\\-/]+";
  return new RegExp(`(\`[^\`\\n]+\`|\\*\\*[^*\\n]+\\*\\*|https?:\\/\\/[^\\s<>()]+|${mention})`, "gi");
}

const PatternContext = createContext<RegExp>(inlinePattern([]));

function InlineText({ text, keyPrefix }: { text: string; keyPrefix: string }) {
  return <>{renderInline(text, keyPrefix, useContext(PatternContext))}</>;
}

function renderInline(text: string, keyPrefix: string, pattern: RegExp): ReactNode[] {
  return text.split(pattern).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (!part) return null;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (/^https?:\/\//.test(part)) {
      return (
        <a key={key} href={part} target="_blank" rel="noreferrer">
          {part}
        </a>
      );
    }
    if (part.startsWith("@") && part.length > 1) {
      return (
        <span key={key} className="mention">
          {part}
        </span>
      );
    }
    return part;
  });
}

function renderBlock(text: string, keyPrefix: string): ReactNode[] {
  return text.split("\n").map((line, i) => {
    const key = `${keyPrefix}-${i}`;
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      return (
        <div key={key} className="rt-heading">
          <InlineText text={heading[1]} keyPrefix={key} />
        </div>
      );
    }
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    if (bullet) {
      return (
        <div key={key} className="rt-bullet">
          <span className="rt-dot">•</span>
          <span>
            <InlineText text={bullet[1]} keyPrefix={key} />
          </span>
        </div>
      );
    }
    const numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      return (
        <div key={key} className="rt-bullet">
          <span className="rt-dot">{numbered[1]}.</span>
          <span>
            <InlineText text={numbered[2]} keyPrefix={key} />
          </span>
        </div>
      );
    }
    if (!line.trim()) return <div key={key} className="rt-gap" />;
    return (
      <div key={key}>
        <InlineText text={line} keyPrefix={key} />
      </div>
    );
  });
}

export function MentionNames({ names, children }: { names: string[]; children: ReactNode }) {
  const key = names.join("\u0000");
  const pattern = useMemo(() => inlinePattern(key ? key.split("\u0000") : []), [key]);
  return <PatternContext.Provider value={pattern}>{children}</PatternContext.Provider>;
}

export default function RichText({ text }: { text: string }) {
  const parts = text.split(/```/);
  return (
    <div className="rich-text">
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const code = part.replace(/^[\w+-]*\n/, "");
          return (
            <pre key={i}>
              <code>{code.replace(/\n$/, "")}</code>
            </pre>
          );
        }
        return <div key={i}>{renderBlock(part.replace(/^\n|\n$/g, ""), `b${i}`)}</div>;
      })}
    </div>
  );
}
