import { useState } from 'react';
import { ApiError } from '../lib/api';

function safeCode(value: string | undefined) { return value && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) ? value : undefined; }
export function diagnosticText(error: unknown): string {
  if (!(error instanceof ApiError)) return '类型：客户端错误';
  return [`HTTP：${error.status}`, safeCode(error.code) ? `错误代码：${error.code}` : '',
    safeCode(error.diagnosticId) ? `诊断编号：${error.diagnosticId}` : ''].filter(Boolean).join('\n');
}
export function DiagnosticError({ error, message }: { error: unknown; message?: string }) {
  const [copyStatus, setCopyStatus] = useState('');
  const text = diagnosticText(error);
  return <div className="error-note" role="alert">
    <p>{message ?? (error instanceof Error ? error.message : String(error))}</p>
    {error instanceof ApiError && safeCode(error.diagnosticId) && <p>诊断编号：{error.diagnosticId}</p>}
    <details><summary>脱敏诊断信息</summary><textarea aria-label="脱敏诊断信息" value={text} readOnly rows={3} /></details>
    <button type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setCopyStatus('诊断信息已复制。'); }
      catch { setCopyStatus('无法访问剪贴板，请展开脱敏诊断信息手动复制。'); }
    }}>复制诊断信息</button>
    {copyStatus && <span role="status">{copyStatus}</span>}
  </div>;
}
