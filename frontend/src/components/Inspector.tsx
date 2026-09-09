import type { ReactNode } from "react";

interface InspectorProps {
  children: ReactNode;
}

export function Inspector({ children }: InspectorProps) {
  return (
    <div className="inspector-content">
      <div className="rail-heading">
        <span>上下文检查器</span>
        <small>随选择更新</small>
      </div>
      {children}
    </div>
  );
}
