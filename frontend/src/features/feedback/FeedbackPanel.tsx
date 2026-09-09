import { useState, type FormEvent } from "react";

export interface FeedbackPayload {
  rating: number;
  tags: string[];
  original_text: string;
  corrected_text: string;
  comment: string;
}

export function FeedbackPanel({
  originalText,
  onSubmit,
}: {
  originalText: string;
  onSubmit: (payload: FeedbackPayload) => Promise<void>;
}) {
  const [rating, setRating] = useState(3);
  const [corrected, setCorrected] = useState(originalText);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || submitted) return;
    setBusy(true);
    setError("");
    try {
      await onSubmit({
        rating,
        tags: [],
        original_text: originalText,
        corrected_text: corrected,
        comment,
      });
      setSubmitted(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="feedback-panel" onSubmit={submit}>
      <header role="group">
        <span className="eyebrow">作者反馈</span>
        <h3>告诉系统哪里不像你的文字</h3>
      </header>
      <label>
        评分
        <select
          aria-label="评分"
          value={rating}
          onChange={(event) => setRating(Number(event.target.value))}
        >
          {[1, 2, 3, 4, 5].map((score) => (
            <option key={score} value={score}>
              {score} 分
            </option>
          ))}
        </select>
      </label>
      <label>
        修正后的文字
        <textarea
          aria-label="修正后的文字"
          value={corrected}
          onChange={(event) => setCorrected(event.target.value)}
        />
      </label>
      <label>
        修改原因
        <textarea
          aria-label="修改原因"
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          placeholder="说清规律，下一次才会更像你。"
        />
      </label>
      <p className="muted">未填写修改原因时，评价与修正仅存档；请在风格实验室补充明确偏好并确认后生效。</p>
      <button type="submit" disabled={busy || submitted}>
        {submitted ? "已提交评价" : "提交评价与修正"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
