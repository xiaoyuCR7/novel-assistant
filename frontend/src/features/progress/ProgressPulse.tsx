interface Progress {
  current_words: number;
  target_words: number;
  completion_ratio: number;
  chapter_count: number;
  completed_chapters: number;
  daily_goal: number;
}

export function ProgressPulse({ progress }: { progress: Progress }) {
  const percent = Math.round(progress.completion_ratio * 100);
  return (
    <section className="progress-pulse" aria-label="创作脉搏">
      <div>
        <span>全书进度</span>
        <strong>
          {progress.current_words.toLocaleString("en-US")} /{" "}
          {progress.target_words.toLocaleString("en-US")} 字
        </strong>
      </div>
      <div
        className="pulse-track"
        role="progressbar"
        aria-label="全书字数进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <div>
        <small>
          {progress.completed_chapters} / {progress.chapter_count} 章完成
        </small>
        <small>每日目标 {progress.daily_goal.toLocaleString("en-US")} 字</small>
      </div>
    </section>
  );
}
