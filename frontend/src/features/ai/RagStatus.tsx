import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, projectApi } from "../../lib/api";
import { Icon } from "../../components/Icon";
import type { VectorState } from "../../lib/types";

const vectorLabels: Record<VectorState, string> = {
  disabled: "未配置向量模型，关键词检索仍可用",
  needs_rebuild: "向量索引待重建，关键词检索仍可用",
  ready: "本机向量索引已就绪",
  degraded: "向量检索暂时不可用，关键词检索仍可用",
};

export function LocalModelNotice() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  return health.data?.ai_provider === "demo" ? (
    <p className="demo-notice workspace-demo">
      离线演示模式：生成使用示例内容与摘录。真实创作需配置本机模型。
    </p>
  ) : null;
}

export function RagStatus({ projectId }: { projectId: string }) {
  const client = projectApi(projectId);
  const queryClient = useQueryClient();
  const health = useQuery({
    queryKey: ["rag", projectId],
    queryFn: client.ragHealth,
  });
  const [busy, setBusy] = useState<"index" | "ledger" | null>(null),
    [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const pending = useRef(false);
  async function rebuild() {
    if (pending.current) return;
    pending.current = true;
    setBusy("index");
    setError("");
    try {
      await client.rebuildRag();
      await health.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      pending.current = false;
      setBusy(null);
    }
  }
  async function repairLedger() {
    if (pending.current) return;
    pending.current = true;
    setBusy("ledger");
    setError("");
    setNotice("");
    try {
      await client.repairProjectLedger();
      await Promise.all(['rag', 'summary', 'jobs'].map(key =>
        queryClient.invalidateQueries({ queryKey: [key, projectId] })));
      const refreshed = queryClient.getQueryState<{ ledger_pending?: boolean }>(['rag', projectId]);
      if (refreshed?.error) throw refreshed.error;
      setNotice(refreshed?.data?.ledger_pending ? "修复请求已处理，账本仍待修复，可稍后重试。" : "本地账本已更新。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      pending.current = false;
      setBusy(null);
    }
  }
  const state = health.data?.vectors;
  return (
    <div className="retrieval-note">
      <Icon name="lock" size={14} />
      <div>
        <strong>本地混合检索</strong>
        <p>
          关键词 + 章节关联
          <br />
          向量检索：
          <span>{state ? vectorLabels[state] : health.error ? "状态不可用" : "读取中…"}</span>
        </p>
        <button
          className="text-action"
          disabled={!!busy}
          onClick={() => void rebuild()}
        >
          {busy === "index" ? "正在重建…" : "重建本项目索引"}
        </button>
        {health.data?.ledger_pending && (
          <div>
            <p role="status">本地连续性账本待修复。仅更新本地文件，不调用模型。</p>
            <button className="text-action" disabled={!!busy} onClick={() => void repairLedger()}>
              仅修复本地账本
            </button>
          </div>
        )}
        {busy === "ledger" && <p role="status">正在修复本地账本…</p>}
        {notice && <p role="status">{notice}</p>}
        {error && <p role="alert">{error}</p>}
      </div>
    </div>
  );
}
