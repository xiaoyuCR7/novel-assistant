import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ModelConfig } from "../../lib/api";
import { DiagnosticError } from '../../components/DiagnosticError';
import { ModelProfiles, modelConfigRevision } from './ModelProfiles';

export function ModelSettingsForm({
  value,
  onSave,
  onTest,
  onDirtyChange,
}: {
  value: ModelConfig;
  onSave: (data: object) => Promise<ModelConfig>;
  onTest: () => Promise<unknown>;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [baselineRevision, setBaselineRevision] = useState(() => modelConfigRevision(value));
  const [mode, setMode] = useState(value.mode),
    [url, setUrl] = useState(value.base_url),
    [model, setModel] = useState(value.model);
  const [key, setKey] = useState(""),
    [consent, setConsent] = useState(value.external_consent),
    [clear, setClear] = useState(false);
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [error, setError] = useState<unknown>(null);
  const [outputBudget, setOutputBudget] = useState(value.output_token_budget ?? 4096),
    [capacity, setCapacity] = useState(value.context_capacity ?? 32768),
    [deadline, setDeadline] = useState(value.deadline_seconds ?? 180),
    [outputParameter, setOutputParameter] = useState(value.output_parameter ?? 'max_tokens'),
    [thinkingMode, setThinkingMode] = useState(value.thinking_mode ?? 'provider_default');
  const changed =
    mode !== value.mode ||
    url !== value.base_url ||
    model !== value.model ||
    consent !== value.external_consent ||
    outputBudget !== (value.output_token_budget ?? 4096) ||
    capacity !== (value.context_capacity ?? 32768) ||
    deadline !== (value.deadline_seconds ?? 180) ||
    outputParameter !== (value.output_parameter ?? 'max_tokens') ||
    thinkingMode !== (value.thinking_mode ?? 'provider_default') ||
    !!key ||
    clear;
  useEffect(() => { onDirtyChange?.(changed); }, [changed, onDirtyChange]);
  async function act(test = false) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (test) {
        await onTest();
        setNotice("连接成功，可以开始创作。");
      } else {
        const saved = await onSave({
          mode,
          base_url: url,
          model,
          api_key: key,
          external_consent: consent,
          clear_api_key: clear,
          output_token_budget: outputBudget,
          context_capacity: capacity,
          deadline_seconds: deadline,
          output_parameter: outputParameter,
          thinking_mode: thinkingMode,
          ...(baselineRevision ? { expected_config_revision: baselineRevision } : {}),
        });
        setMode(saved.mode);
        setUrl(saved.base_url);
        setModel(saved.model);
        setConsent(saved.external_consent);
        setOutputBudget(saved.output_token_budget ?? 4096);
        setCapacity(saved.context_capacity ?? 32768);
        setDeadline(saved.deadline_seconds ?? 180);
        setOutputParameter(saved.output_parameter ?? 'max_tokens');
        setThinkingMode(saved.thinking_mode ?? 'provider_default');
        setBaselineRevision(modelConfigRevision(saved));
        setKey("");
        setClear(false);
        setNotice("设置已保存，新请求将使用此配置。");
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="model-settings">
      <header>
        <h1>模型与连接</h1>
        <p>本机通用的模型连接。小说和对话分别保存，互不混用。</p>
      </header>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void act();
        }}
      >
        <fieldset disabled={busy}>
          <label>
            运行模式
            <select
              aria-label="运行模式"
              value={mode}
              onChange={(e) => {
                const next = e.target.value as ModelConfig["mode"];
                setMode(next);
                setConsent(false);
                setKey("");
                setUrl(
                  next === "api"
                    ? "https://api.openai.com/v1"
                    : next === "local"
                      ? "http://127.0.0.1:11434"
                      : "",
                );
                setModel("");
              }}
            >
              <option value="demo">离线演示</option>
              <option value="local">本机模型 · Ollama</option>
              <option value="api">API 服务 · OpenAI 兼容</option>
            </select>
          </label>
          {mode !== "demo" && (
            <>
              <label>
                {mode === "api" ? "API 地址" : "本机服务地址"}
                <input
                  aria-label="API 地址"
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                    setConsent(false);
                    setKey("");
                  }}
                  placeholder="https://api.example.com/v1"
                  required
                />
              </label>
              <label>
                模型名称
                <input
                  aria-label="模型名称"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="填写服务商提供的准确模型名称"
                  required
                />
              </label>
            </>
          )}
          {mode === "api" && (
            <>
              <label>
                API Key
                <input
                  type="password"
                  aria-label="API Key"
                  value={key}
                  autoComplete="new-password"
                  spellCheck={false}
                  onChange={(e) => {
                    setKey(e.target.value);
                    setClear(false);
                  }}
                  placeholder={
                    value.has_api_key && !clear
                      ? "已安全保存，留空保持不变"
                      : "输入你的 API Key"
                  }
                />
              </label>
              <p className="subtle">
                密钥仅在本机后端加密保存，不进入对话、参考资料或项目备份。
              </p>
              <label className="consent-row">
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                />
                <span>
                  允许将当前任务的对话、正文及相关资料发送给此 API
                  服务商。可能产生费用。
                </span>
              </label>
            </>
          )}
          {mode === "local" && (
            <p className="settings-note">
              仅连接本机回环地址，不自动下载模型。请先启动 Ollama 并安装模型。
            </p>
          )}
          {mode === "demo" && (
            <p className="settings-note">
              无需密钥，不联网。回复为示例内容，不代表真实 AI 写作能力。
            </p>
          )}
          <details>
            <summary>生成限制与兼容性</summary>
            <p className="subtle">新任务默认使用模型上下文容量减去输出预留，也可在对话输入框旁设置较小的省费上限。请按服务商规格填写模型容量，系统不会按模型名称猜测。发送前仅本地估算首阶段必要输入；预检不保证后续阶段可容纳，测试连接使用已保存的限制。</p>
            <label>最大输出 Token<input aria-label="最大输出 Token" type="number" min={256} max={65536} required
              value={outputBudget} onChange={e => setOutputBudget(Number(e.target.value))} /></label>
            <label>模型上下文容量<input aria-label="模型上下文容量" type="number" min={1024} max={1048576} required
              value={capacity} onChange={e => setCapacity(Number(e.target.value))} /></label>
            <label>请求超时秒数<input aria-label="请求超时秒数" type="number" min={1} max={600} required
              value={deadline} onChange={e => setDeadline(Number(e.target.value))} /></label>
            <label>输出限制参数<select aria-label="输出限制参数" value={outputParameter}
              onChange={e => setOutputParameter(e.target.value as 'max_tokens' | 'max_completion_tokens')}>
              <option value="max_tokens">max_tokens</option>
              <option value="max_completion_tokens">max_completion_tokens</option>
            </select></label>
            <label>思考模式<select aria-label="思考模式" value={thinkingMode}
              onChange={e => setThinkingMode(e.target.value as NonNullable<ModelConfig['thinking_mode']>)}>
              <option value="provider_default">服务商默认（不发送兼容参数）</option>
              <option value="disabled">关闭（节省推理 Token）</option>
              <option value="enabled">开启</option>
            </select></label>
            <p className="subtle">仅在明确选择开启或关闭时发送兼容 API 的 thinking 参数；不支持该参数的服务商请保持默认。</p>
          </details>
          <div className="settings-actions">
            <button
              className="primary-action"
              type="submit"
              disabled={busy || (mode === "api" && !consent)}
            >
              保存设置
            </button>
            <button
              type="button"
              disabled={busy || changed}
              onClick={() => void act(true)}
            >
              测试连接
            </button>
            {value.has_api_key && (
              <button
                type="button"
                className="danger-text"
                onClick={() => {
                  setClear(true);
                  setKey("");
                  setMode("demo");
                  setConsent(false);
                  setNotice("点击保存设置以移除密钥并切回演示模式。");
                }}
              >
                移除密钥
              </button>
            )}
          </div>
          {changed && (
            <p className="subtle">
              先保存，再测试。测试只发送固定测试句，不含小说内容。
            </p>
          )}
        </fieldset>
        {notice && (
          <p role="status" className="settings-success">
            {notice}
          </p>
        )}
        {!!error && <DiagnosticError error={error} />}
      </form>
    </section>
  );
}

export function ModelSettings() {
  const cache = useQueryClient();
  const [repairing, setRepairing] = useState(false);
  const [repairError, setRepairError] = useState<unknown>(null);
  const [formDirty, setFormDirty] = useState(false);
  const [formVersion, setFormVersion] = useState(0);
  const config = useQuery({
    queryKey: ["model-settings"],
    queryFn: api.modelSettings,
  });
  if (config.error && !config.data)
    return (
      <div>
        <DiagnosticError error={config.error} message={`无法读取设置：${config.error.message}`} />
        <button onClick={() => config.refetch()}>重试</button>
        {config.error instanceof ApiError && config.error.code === "MODEL_CONFIG_UNAVAILABLE" && (
          <button
            disabled={repairing}
            onClick={async () => {
              if (!window.confirm("备份损坏配置后重置为离线演示？当前密钥将移出使用中的配置，需要重新填写；小说内容不会改变。")) return;
              setRepairing(true);
              setRepairError("");
              try {
                const saved = await api.saveModelSettings({
                  mode: "demo", clear_api_key: true, repair_config: true,
                });
                await cache.cancelQueries({ queryKey: ["model-settings"], exact: true });
                cache.setQueryData(["model-settings"], saved);
              } catch (e) {
                setRepairError(e);
              } finally {
                setRepairing(false);
              }
            }}
          >
            备份并重置配置
          </button>
        )}
        {!!repairError && <DiagnosticError error={repairError} />}
      </div>
    );
  if (!config.data) return <p>正在读取模型设置…</p>;
  return (
    <>
    {config.error && <DiagnosticError error={config.error} message={`设置刷新失败，未保存输入已保留：${config.error.message}`} />}
    <ModelSettingsForm
      key={formVersion}
      value={config.data}
      onDirtyChange={setFormDirty}
      onTest={api.testModel}
      onSave={async (data) => {
        const saved = await api.saveModelSettings(data);
        await cache.cancelQueries({ queryKey: ["model-settings"], exact: true });
        cache.setQueryData(["model-settings"], saved);
        await cache.cancelQueries({ queryKey: ["model-profiles"], exact: true });
        await cache.invalidateQueries({ queryKey: ["model-profiles"] });
        await cache.invalidateQueries({ queryKey: ["health"] });
        return saved;
      }}
    />
    {formDirty && <button type="button" onClick={() => { setFormVersion(value => value + 1); setFormDirty(false); }}>放弃未保存的模型设置</button>}
    <ModelProfiles value={config.data} beforeApply={() => {
      if (formDirty) throw Error('请先保存或放弃未保存的模型设置，再操作配置方案。');
      return true;
    }} onApplied={() => { setFormVersion(value => value + 1); setFormDirty(false); }} />
    </>
  );
}
