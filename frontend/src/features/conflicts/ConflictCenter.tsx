import { useState } from "react";
import type { Conflict } from "../../lib/api";

const modeLabel: Record<string, string> = {
  conservative: "保守",
  balanced: "平衡",
  radical: "激进",
};

export function ConflictCenter({
  conflicts,
  onDecide,
  onResolveEntityState,
}: {
  conflicts: Conflict[];
  onDecide: (
    conflictId: string,
    optionId: string,
    note: string,
  ) => Promise<void>;
  onResolveEntityState: (
    conflictId: string,
    stateId: string,
    revision: number,
    note: string,
  ) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  return (
    <section className="conflict-center">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">冲突中心</span>
          <h2>问题不是红灯，是分岔路</h2>
        </div>
      </header>
      {conflicts.map((conflict) => (
        <article
          key={conflict.id}
          className={`conflict-card conflict-card--${conflict.severity}`}
        >
          <header role="group">
            <span>{conflict.code}</span>
            <strong>{conflict.message}</strong>
          </header>
          {conflict.code === "ENTITY_STATE_CONFLICT" && conflict.entity_state_resolution ? (
            <>
              <p>选择一条不再成立的状态记录撤回；系统会重新检查本章，仍有矛盾时冲突会保持开启。</p>
              <div className="resolution-options">
                {conflict.entity_state_resolution.states.map((state) => {
                  const stateData = JSON.stringify(state.data);
                  return (
                    <label key={state.id} className="resolution-option resolution-option--conservative">
                      <input
                        type="radio"
                        name={`conflict-${conflict.id}`}
                        aria-label={`撤回状态 ${stateData}`}
                        checked={selected[conflict.id] === state.id}
                        onChange={() => setSelected((current) => ({
                          ...current,
                          [conflict.id]: state.id,
                        }))}
                      />
                      <span>
                        <small>人物 {state.entity_id}</small>
                        <strong>{stateData}</strong>
                        <p>生效节点：{state.valid_from_node_id}</p>
                        <p>结束节点：{state.valid_to_node_id ?? "持续有效"}</p>
                      </span>
                    </label>
                  );
                })}
              </div>
              <label>
                状态冲突处理说明
                <textarea
                  aria-label="状态冲突处理说明"
                  value={notes[conflict.id] ?? ""}
                  onChange={(event) => setNotes((current) => ({
                    ...current,
                    [conflict.id]: event.target.value,
                  }))}
                />
              </label>
              <button
                type="button"
                disabled={!selected[conflict.id]}
                onClick={() => {
                  const state = conflict.entity_state_resolution?.states.find(
                    (item) => item.id === selected[conflict.id],
                  );
                  if (state) {
                    void onResolveEntityState(
                      conflict.id,
                      state.id,
                      state.revision,
                      notes[conflict.id] ?? "",
                    );
                  }
                }}
              >
                撤回所选状态并重新检查
              </button>
            </>
          ) : (
            <>
              <div className="resolution-options">
                {conflict.options.map((option) => (
              <label
                key={option.id}
                className={`resolution-option resolution-option--${option.mode}`}
              >
                <input
                  type="radio"
                  name={`conflict-${conflict.id}`}
                  aria-label={`${modeLabel[option.mode]}：${option.title}`}
                  checked={selected[conflict.id] === option.id}
                  onChange={() =>
                    setSelected((state) => ({
                      ...state,
                      [conflict.id]: option.id,
                    }))
                  }
                />
                <span>
                  <small>{modeLabel[option.mode]}</small>
                  <strong>{option.title}</strong>
                  <p>收益：{option.benefit}</p>
                  <p>风险：{option.risk}</p>
                  <em>{option.ripple_effects.join(" · ")}</em>
                </span>
              </label>
                ))}
              </div>
              <label>
                决定说明
                <textarea
                  aria-label="决定说明"
                  value={notes[conflict.id] ?? ""}
                  onChange={(event) => setNotes((current) => ({
                    ...current,
                    [conflict.id]: event.target.value,
                  }))}
                />
              </label>
              <button
                type="button"
                disabled={!selected[conflict.id]}
                onClick={() => onDecide(
                  conflict.id,
                  selected[conflict.id],
                  notes[conflict.id] ?? "",
                )}
              >
                记录解决决定
              </button>
            </>
          )}
        </article>
      ))}
    </section>
  );
}
