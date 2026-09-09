// Historical console retained for compatibility tests; current UI is ChatWorkspace.
import { useState } from "react";

import { ContextPreview, type ContextSnapshot } from "./ContextPreview";

interface AIJob {
  id: string;
  status: string;
  task_type: string;
  error_message?: string;
  result: { candidate_text?: string; stage_order?: string[] };
}

interface DirectorConsoleProps {
  context: ContextSnapshot;
  job: AIJob | null;
  running: boolean;
  onRun: (task: string, instructions: string) => Promise<void>;
  onAccept: (jobId: string) => Promise<void>;
}

export function DirectorConsole({
  context,
  job,
  running,
  onRun,
  onAccept,
}: DirectorConsoleProps) {
  const [showContext, setShowContext] = useState(false);
  const [error, setError] = useState("");
  async function act(action: () => Promise<void>) {
    try {
      setError("");
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const [instructions, setInstructions] = useState(
    "保持人物主动性，让威胁隐藏在可见动作里。",
  );

  return (
    <section className="director-console">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">AI 导演台</span>
          <h2>建议有边界，正文有主笔</h2>
        </div>
        <span className={`job-state job-state--${job?.status ?? "idle"}`}>
          {running ? "生成中" : (job?.status ?? "待命")}
        </span>
      </header>
      <label className="director-instruction">
        本次导演要求
        <textarea
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
        />
      </label>
      <div className="director-actions">
        <button
          type="button"
          onClick={() => void act(() => onRun("plan", instructions))}
          disabled={running}
        >
          规划场景
        </button>
        <button
          type="button"
          onClick={() => void act(() => onRun("draft", instructions))}
          disabled={running}
        >
          生成初稿
        </button>
        <button
          type="button"
          onClick={() =>
            void act(() => onRun("scene_description", instructions))
          }
          disabled={running}
        >
          生成场景描写
        </button>
        <button
          type="button"
          onClick={() => void act(() => onRun("review", instructions))}
          disabled={running}
        >
          检查连续性
        </button>
        <button
          className="primary-action"
          type="button"
          onClick={() => void act(() => onRun("full_chapter", instructions))}
          disabled={running}
        >
          生成并统一重写
        </button>
      </div>
      {(error || job?.error_message) && (
        <p role="alert">{error || job?.error_message}</p>
      )}
      <button
        className="text-action"
        type="button"
        onClick={() => setShowContext((shown) => !shown)}
      >
        查看本次上下文
      </button>
      {showContext && <ContextPreview context={context} />}
      {job?.result.candidate_text && (
        <article className="candidate-manuscript">
          <header role="group">
            <span>候选正文</span>
            <small>尚未进入工作副本</small>
          </header>
          <p>{job.result.candidate_text}</p>
          <button
            className="primary-action"
            type="button"
            onClick={() => void act(() => onAccept(job.id))}
          >
            接受候选并创建版本
          </button>
        </article>
      )}
    </section>
  );
}
