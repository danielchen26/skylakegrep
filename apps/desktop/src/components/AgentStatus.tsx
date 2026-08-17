import { Network } from "lucide-react";
import type { PointerEvent } from "react";
import type { AgentPhase, SearchQuality } from "../types";

const phaseOrder: AgentPhase[] = ["search", "analyze", "synthesize", "output"];

type AgentStatusProps = {
  phase: AgentPhase;
  progress: number;
  quality: SearchQuality;
  running: boolean;
  onDragStart: (event: PointerEvent<HTMLElement>) => void;
};

export function AgentStatus({
  phase,
  progress,
  quality,
  running,
  onDragStart,
}: AgentStatusProps) {
  const activeIndex = phaseOrder.indexOf(phase);

  return (
    <section className="agent-status glass-panel">
      <div className="native-drag-strip" data-tauri-drag-region onPointerDown={onDragStart} />
      <div className="agent-copy">
        <Network size={30} />
        <div>
          <strong>Skygrep Agent</strong>
          <span>{running ? "Working..." : quality === "uncertain" ? "Needs review" : "Ready"}</span>
        </div>
      </div>
      <div className="phase-track">
        <div className="track-line">
          <span style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
        {phaseOrder.map((item, index) => (
          <div className={`phase-dot ${index <= activeIndex ? "active" : ""}`} key={item}>
            <span />
            <em>{item[0].toUpperCase() + item.slice(1)}</em>
          </div>
        ))}
      </div>
    </section>
  );
}
