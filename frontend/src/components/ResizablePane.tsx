import { useState } from "react";
export function usePaneWidth(
  projectId: string,
  name: string,
  initial: number,
  min: number,
  max: number,
) {
  const key = `studio:${projectId}:${name}-width`;
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(key));
      return saved ? Math.max(min, Math.min(max, saved)) : initial;
    } catch {
      return initial;
    }
  });
  function resize(value: number) {
    const next = Math.max(min, Math.min(max, value));
    setWidth(next);
    try {
      localStorage.setItem(key, String(next));
    } catch {
      /* Layout remains usable without storage. */
    }
  }
  return { width, resize, min, max };
}
export function ResizablePane({
  label,
  pane,
  direction = 1,
}: {
  label: string;
  pane: ReturnType<typeof usePaneWidth>;
  direction?: number;
}) {
  return (
    <div
      className="pane-separator"
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={pane.min}
      aria-valuemax={pane.max}
      aria-valuenow={pane.width}
      tabIndex={0}
      onKeyDown={(e) => {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
          e.preventDefault();
          pane.resize(
            e.key === "Home"
              ? pane.min
              : e.key === "End"
                ? pane.max
                : pane.width + (e.key === "ArrowRight" ? 8 : -8) * direction,
          );
        }
      }}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture?.(e.pointerId);
        e.currentTarget.dataset.origin = String(e.clientX);
        e.currentTarget.dataset.width = String(pane.width);
      }}
      onPointerMove={(e) => {
        if (e.currentTarget.hasPointerCapture?.(e.pointerId))
          pane.resize(
            Number(e.currentTarget.dataset.width) +
              (e.clientX - Number(e.currentTarget.dataset.origin)) * direction,
          );
      }}
      onPointerUp={(e) => e.currentTarget.releasePointerCapture?.(e.pointerId)}
    />
  );
}
