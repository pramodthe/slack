import type { MessageComponent } from "../types";
import { Chart } from "./assistant-ui/elements/chart";
import { DataTable } from "./assistant-ui/elements/data-table";
import { SpecSheet } from "./assistant-ui/elements/spec-sheet";

function Component({ component }: { component: MessageComponent }) {
  switch (component.kind) {
    case "table":
      return <DataTable {...component.props} />;
    case "chart":
      return <Chart {...component.props} />;
    case "spec":
      return <SpecSheet {...component.props} />;
  }
}

export default function MessageComponents({ components }: { components: MessageComponent[] }) {
  return (
    <div className="flex max-w-2xl flex-col gap-2 pt-1.5">
      {components.map((component) => (
        <Component key={component.id} component={component} />
      ))}
    </div>
  );
}
