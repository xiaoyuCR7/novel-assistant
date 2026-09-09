import { useState } from "react";
import { Modal } from "../../components/Modal";
export interface DriftAlert {
  id: string;
  severity: string;
  message: string;
  status: string;
  evidence?: string[];
}

const dimensionLabels: Record<string, string> = {
  theme_alignment: "主题一致性",
  chapter_purpose: "章节目的",
  plot_progression: "剧情推进",
  character_motivation: "人物动机",
  pov_and_voice: "视角与声音",
  continuity: "连续性",
  pacing_and_focus: "节奏与焦点",
};

function displayEvidence(evidence: string[] = []) {
  const value = (prefix: string) => evidence.find(item => item.startsWith(prefix))?.slice(prefix.length);
  const dimension = value("dimension:");
  const visible = evidence.filter(item => ![
    "content_check:", "ai_job:", "document_revision:", "dimension:",
    "range:", "quote:", "suggestion:", "reference:",
  ].some(prefix => item.startsWith(prefix)));
  return {
    dimension: dimension ? dimensionLabels[dimension] ?? dimension : undefined,
    quote: value("quote:"),
    suggestion: value("suggestion:"),
    visible,
  };
}
export function DriftAlertCenter({
  alerts,
  onDecide,
}: {
  alerts: DriftAlert[];
  onDecide: (
    id: string,
    decision: "accept" | "dismiss",
    confirmed: boolean,
  ) => Promise<void>;
}) {
  const [confirm, setConfirm] = useState<DriftAlert | null>(null),
    [error, setError] = useState("");
  async function decide(
    alert: DriftAlert,
    decision: "accept" | "dismiss",
    confirmed = false,
  ) {
    try {
      await onDecide(alert.id, decision, confirmed);
      setConfirm(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <section className="drift-center">
      {error && <p role="alert">{error}</p>}
      {alerts
        .filter((a) => a.status === "open")
        .map((alert) => {
          const evidence = displayEvidence(alert.evidence);
          return (
          <article
            className={`drift-alert severity-${alert.severity}`}
            key={alert.id}
          >
            <span className="badge">
              {{
                info: "提示",
                warning: "注意",
                severe: "严重偏离",
                error: "严重偏离",
              }[alert.severity] ?? "提醒"}{" "}
              · 作者决定
            </span>
            {evidence.dimension && <small className="content-check-dimension">{evidence.dimension}</small>}
            <p>{alert.message}</p>
            {evidence.quote && <small>原文：{evidence.quote}</small>}
            {evidence.suggestion && <small>建议：{evidence.suggestion}</small>}
            {evidence.visible.map((e, i) => (
              <small key={i}>{e}</small>
            ))}
            <div>
              <button onClick={() => void decide(alert, "dismiss")}>
                关闭提醒
              </button>
              <button
                onClick={() => {
                  if (["error", "severe"].includes(alert.severity))
                    setConfirm(alert);
                  else void decide(alert, "accept");
                }}
              >
                仍按作者决定继续
              </button>
            </div>
          </article>
          );
        })}
      {confirm && (
        <Modal title="确认忽略严重偏离" onClose={() => setConfirm(null)}>
          <p>{confirm.message}</p>
          <p>此操作仅记录你的决定，不会自动修改正文或设定。</p>
          <button
            className="primary-action"
            onClick={() => void decide(confirm, "accept", true)}
          >
            确认继续
          </button>
        </Modal>
      )}
    </section>
  );
}
