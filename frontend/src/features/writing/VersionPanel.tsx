import { useEffect, useRef, useState } from "react";

import type { VersionSummary } from "../../lib/api";

const versionLabel = (version: VersionSummary) => version.summary || version.source || version.id;

interface VersionPanelProps {
  versions: VersionSummary[];
  onCompare: (fromId: string, toId: string) => Promise<string>;
  onRestore: (versionId: string) => Promise<void>;
  hasMore?: boolean;
  initialLoading?: boolean;
  loadError?: string;
  retrying?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  onRetry?: () => void;
  busy?: boolean;
}

export function VersionPanel({
  versions,
  onCompare,
  onRestore,
  hasMore = false,
  initialLoading = false,
  loadError,
  retrying = false,
  loadingMore = false,
  onLoadMore,
  onRetry,
  busy = false,
}: VersionPanelProps) {
  const [fromSelection, setFromId] = useState("");
  const [toSelection, setToId] = useState("");
  const fromId = versions.some(v => v.id === fromSelection) ? fromSelection : versions[0]?.id ?? "";
  const toId = versions.some(v => v.id === toSelection) ? toSelection : versions[1]?.id ?? versions[0]?.id ?? "";
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  const [restorePending, setRestorePending] = useState(false);
  const [comparePending, setComparePending] = useState(false);
  const restoring = useRef(false);
  const loadingMoreLock = useRef(false);
  const compareGeneration = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    if (!loadingMore) loadingMoreLock.current = false;
  }, [loadingMore]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      compareGeneration.current += 1;
    };
  }, []);
  useEffect(() => {
    compareGeneration.current += 1;
    setComparePending(false);
    setDiff("");
    setError("");
  }, [fromId, toId]);

  const changeComparison = (side: "from" | "to", value: string) => {
    compareGeneration.current += 1;
    setComparePending(false);
    setDiff("");
    setError("");
    if (side === "from") setFromId(value);
    else setToId(value);
  };

  return (
    <section className="version-panel" data-version-list-mode="paged-metadata-v1">
      <header role="group">
        <span className="eyebrow">版本链</span>
        <h3>历史不可改写</h3>
        <p>恢复会创建新版本，不改写历史。</p>
      </header>
      <div className="version-compare">
        <label>
          基准版本
          <select
            aria-label="基准版本"
            value={fromId}
            onChange={(event) => changeComparison("from", event.target.value)}
          >
            {versions.map((version) => (
              <option key={version.id} value={version.id}>
                {versionLabel(version)}
              </option>
            ))}
          </select>
        </label>
        <label>
          比较版本
          <select
            aria-label="比较版本"
            value={toId}
            onChange={(event) => changeComparison("to", event.target.value)}
          >
            {versions.map((version) => (
              <option key={version.id} value={version.id}>
                {versionLabel(version)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={async () => {
            if (comparePending) return;
            const comparedFromId = fromId;
            const comparedToId = toId;
            const generation = ++compareGeneration.current;
            setComparePending(true);
            setDiff("");
            setError("");
            try {
              const result = await onCompare(comparedFromId, comparedToId);
              if (mounted.current && generation === compareGeneration.current) setDiff(result);
            } catch (e) {
              if (mounted.current && generation === compareGeneration.current) {
                setError(e instanceof Error ? e.message : String(e));
              }
            } finally {
              if (mounted.current && generation === compareGeneration.current) setComparePending(false);
            }
          }}
          disabled={!fromId || !toId || comparePending}
        >
          {comparePending ? "比较中…" : "比较版本"}
        </button>
      </div>
      {diff && <pre className="version-diff">{diff}</pre>}
      {error && <p role="alert">{error}</p>}
      {initialLoading && versions.length === 0 && <p role="status">正在加载历史版本…</p>}
      {loadError && (
        <p role="alert">
          {loadError}
          {onRetry && (
            <button type="button" disabled={retrying} onClick={onRetry}>
              {retrying ? "重试中…" : "重试加载版本"}
            </button>
          )}
        </p>
      )}
      <ol className="version-list">
        {versions.map((version) => (
          <li key={version.id}>
            <div>
              <strong>{versionLabel(version)}</strong>
              <small>{version.source} · {version.created_at}</small>
            </div>
            <button type="button" disabled={busy || restorePending || comparePending} onClick={async () => {
              if (busy || comparePending || restoring.current) return;
              restoring.current = true;
              setRestorePending(true);
              setError("");
              try { await onRestore(version.id); }
              catch (e) { setError(e instanceof Error ? e.message : String(e)); }
              finally { restoring.current = false; setRestorePending(false); }
            }}>
              从{versionLabel(version)}创建恢复版本
            </button>
          </li>
        ))}
      </ol>
      {hasMore && onLoadMore && !loadError && (
        <button
          type="button"
          disabled={loadingMore}
          onClick={() => {
            if (loadingMore || loadingMoreLock.current) return;
            loadingMoreLock.current = true;
            onLoadMore();
          }}
        >
          {loadingMore ? "加载中…" : "加载更多"}
        </button>
      )}
    </section>
  );
}
