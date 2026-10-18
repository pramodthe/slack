import { Suspense, createContext, lazy, useContext, useMemo, useState, type ReactNode } from "react";
import { Sources } from "./assistant-ui/elements/sources";
import { hostOf } from "./assistant-ui/utils/href";

const SyntaxHighlighter = lazy(() =>
  import("./assistant-ui/elements/shiki-highlighter").then((m) => ({ default: m.SyntaxHighlighter })),
);

const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const URL_PATTERN = "https?:\\/\\/[^\\s<>()]+";

function inlinePattern(names: string[]): RegExp {
  const known = [...names]
    .filter((n) => /\s/.test(n))
    .sort((a, b) => b.length - a.length)
    .map(escapeRegex);
  const mention = known.length ? `@(?:${known.join("|")})|@[\\w.\\-/]+` : "@[\\w.\\-/]+";
  return new RegExp(`(\`[^\`\\n]+\`|\\*\\*[^*\\n]+\\*\\*|${URL_PATTERN}|${mention})`, "gi");
}

export const mentionClass = "bg-mention-bg text-mention rounded-[3px] px-0.5 font-semibold";

const PatternContext = createContext<RegExp>(inlinePattern([]));

function InlineText({ text, keyPrefix }: { text: string; keyPrefix: string }) {
  return <>{renderInline(text, keyPrefix, useContext(PatternContext))}</>;
}

function renderInline(text: string, keyPrefix: string, pattern: RegExp): ReactNode[] {
  return text.split(pattern).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (!part) return null;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      return (
        <code
          key={key}
          className="rounded-[3px] border border-[#e0e0e0] bg-[#f4f4f4] px-1 py-px font-mono text-[0.85em] text-[#c01343]"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (/^https?:\/\//.test(part)) {
      return (
        <a key={key} href={part} target="_blank" rel="noreferrer" className="text-mention hover:underline">
          {part}
        </a>
      );
    }
    if (part.startsWith("@") && part.length > 1) {
      return (
        <span key={key} className={mentionClass}>
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
        <div key={key} className="mt-1 font-black">
          <InlineText text={heading[1]} keyPrefix={key} />
        </div>
      );
    }
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      return (
        <div key={key} className="flex gap-2 pl-1.5">
          <span className="text-muted-foreground min-w-3">{bullet ? "•" : `${numbered![1]}.`}</span>
          <span>
            <InlineText text={bullet ? bullet[1] : numbered![2]} keyPrefix={key} />
          </span>
        </div>
      );
    }
    if (!line.trim()) return <div key={key} className="h-1.5" />;
    return (
      <div key={key}>
        <InlineText text={line} keyPrefix={key} />
      </div>
    );
  });
}

const codeBox =
  "my-1.5 [&_pre]:m-0 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-[#ddd] [&_pre]:bg-muted! [&_pre]:px-3 [&_pre]:py-2.5 [&_pre]:font-mono [&_pre]:text-[13px] [&_pre]:leading-relaxed [&_pre_code]:border-0 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[length:inherit] [&_pre_code]:text-inherit";

function CodeBlock({ code, language, streaming }: { code: string; language: string; streaming: boolean }) {
  const plain = (
    <div className={codeBox}>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
  return (
    <Suspense fallback={plain}>
      <SyntaxHighlighter className={codeBox} code={code} language={language || "text"} streaming={streaming} />
    </Suspense>
  );
}

export function MentionNames({ names, children }: { names: string[]; children: ReactNode }) {
  const key = names.join("\u0000");
  const pattern = useMemo(() => inlinePattern(key ? key.split("\u0000") : []), [key]);
  return <PatternContext.Provider value={pattern}>{children}</PatternContext.Provider>;
}

export function linksIn(text: string): string[] {
  return [...new Set(text.match(new RegExp(URL_PATTERN, "g")) ?? [])];
}

export function SourcesPill({ links }: { links: string[] }) {
  const [open, setOpen] = useState(false);
  const sources = links.map((url) => ({ url, domain: hostOf(url), title: url.replace(/^https?:\/\/(www\.)?/, "") }));
  return (
    <div className="tool-ui pt-1.5">
      <Sources className="max-w-md" sources={sources} open={open} onOpenChange={setOpen} layout="list" />
    </div>
  );
}

export default function RichText({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const parts = text.split(/```/);
  return (
    <div data-slot="rich-text" className="leading-[1.46] [overflow-wrap:anywhere]">
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const language = /^([\w+-]*)\n/.exec(part)?.[1] ?? "";
          const code = part.replace(/^[\w+-]*\n/, "").replace(/\n$/, "");
          return <CodeBlock key={i} code={code} language={language} streaming={streaming} />;
        }
        return <div key={i}>{renderBlock(part.replace(/^\n|\n$/g, ""), `b${i}`)}</div>;
      })}
    </div>
  );
}
