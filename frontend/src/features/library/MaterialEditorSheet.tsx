import { useState } from "react";
import { Modal } from "../../components/Modal";
import type { LibraryItem, MemoryConflict } from "../../lib/types";
import { ApiError } from "../../lib/api";
export const categoryNames: Record<string, string> = {
  entity: "人物与世界",
  relation: "人物关系",
  node: "情节大纲",
  canon: "确认事实",
  timeline: "时间线",
  plot: "情节与伏笔",
  idea: "灵感与设定",
  style: "风格方案",
  style_rule: "风格规则",
  summary: "连续性账本",
  manuscript: "正文版本",
  asset: "视觉素材",
};
const typedFields: Record<string, Record<string, unknown>> = {
  entity: { profile: {}, state: {} },
  plot: { status: 'active', payoff: '', start_node_id: null, due_node_id: null },
  canon: { subject_entity_id: null, valid_from_node_id: null, valid_to_node_id: null, status: 'confirmed', source_note: '' },
  node: { parent_id: null, order_index: 0, target_words: 0, pov_entity_id: null },
  style: { config: {} },
};
const fieldHints: Record<string, string> = {
  entity: 'profile 为人物档案对象；state 为当前状态对象。',
  plot: 'status 为情节状态；payoff 为回收内容；start_node_id / due_node_id 为起止章节 ID。',
  canon: 'subject_entity_id 为主体 ID；valid_from_node_id / valid_to_node_id 为生效范围；status 为 pending / confirmed / retracted；source_note 为依据。',
  node: 'parent_id 为父节点 ID；order_index / target_words 为非负整数；pov_entity_id 为视角人物 ID。章节状态由完成流程管理。',
  style: 'config 为风格配置对象，例如 distance、rhythm、forbidden_phrases。启用状态由上方开关控制。',
};
function initialFields(item: LibraryItem | null) {
  return JSON.stringify(Object.fromEntries(Object.entries(typedFields[item?.type ?? ''] ?? {})
    .map(([key, fallback]) => [key, item?.record[key] ?? fallback])), null, 2);
}
function profileAliases(profile: unknown) {
  if (!profile || typeof profile !== 'object' || Array.isArray(profile)) return '';
  const aliases = (profile as Record<string, unknown>).aliases;
  return Array.isArray(aliases) ? aliases.filter((alias): alias is string => typeof alias === 'string').join('\n')
    : typeof aliases === 'string' ? aliases : '';
}
function parseAliases(value: string) {
  return [...new Set(value.split(/[\n,，、]/).map(alias => alias.trim()).filter(Boolean))];
}
export function MaterialEditorSheet({
  item,
  onSave,
  onDelete,
  onClose,
  conflicts = [],
  conflictsLoading = false, conflictsError, onRetryConflicts,
}: {
  item: LibraryItem | null;
  onSave: (kind: string, data: Record<string, unknown>) => Promise<void>;
  onDelete: () => Promise<void> | void;
  onClose: () => void;
  conflicts?: MemoryConflict[];
  conflictsLoading?: boolean;
  conflictsError?: string;
  onRetryConflicts?: () => void;
}) {
  const [kind, setKind] = useState(item?.type ?? "entity"),
    [title, setTitle] = useState(item?.title ?? ""),
    [content, setContent] = useState(item?.content ?? "");
  const [fieldsText, setFieldsText] = useState(() => initialFields(item)),
    [active, setActive] = useState(item?.record.is_active === true),
    [pinned, setPinned] = useState(item?.is_pinned ?? false);
  const [aliases, setAliases] = useState(() => profileAliases(item?.record.profile));
  const [aliasesChanged, setAliasesChanged] = useState(false);
  const [revision, setRevision] = useState(item?.revision ?? 1),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false),
    [conflict, setConflict] = useState<LibraryItem | null>(null);
  function changeAliases(value: string) {
    setAliases(value);
    setAliasesChanged(true);
    try {
      const parsed: unknown = JSON.parse(fieldsText);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return;
      const fields = parsed as Record<string, unknown>;
      const profile = fields.profile === undefined ? item?.record.profile ?? {} : fields.profile;
      if (!profile || typeof profile !== 'object' || Array.isArray(profile)) return;
      fields.profile = { ...profile, aliases: parseAliases(value) };
      setFieldsText(JSON.stringify(fields, null, 2));
    } catch { /* Keep incomplete JSON intact; save still validates it. */ }
  }
  function changeFields(value: string) {
    setFieldsText(value);
    if (kind !== 'entity') return;
    try {
      const parsed: unknown = JSON.parse(value);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return;
      const fields = parsed as Record<string, unknown>;
      const profile = fields.profile === undefined ? item?.record.profile : fields.profile;
      if (profile !== undefined && (!profile || typeof profile !== 'object' || Array.isArray(profile))) return;
      setAliases(profileAliases(profile));
      setAliasesChanged(false);
    } catch { /* Preserve the last aliases while the JSON edit is incomplete. */ }
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      let fields: Record<string, unknown> = {};
      if (item && typedFields[kind]) {
        const parsed: unknown = JSON.parse(fieldsText);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw Error('类型字段必须是 JSON 对象。');
        fields = parsed as Record<string, unknown>;
        if (Object.keys(fields).some(key => !(key in typedFields[kind]))) throw Error('存在不支持的类型字段，请按字段提示填写。');
        if (kind === 'entity' && aliasesChanged) {
          const profile = fields.profile === undefined ? {} : fields.profile;
          if (!profile || typeof profile !== 'object' || Array.isArray(profile)) throw Error('人物档案 profile 必须是 JSON 对象。');
          fields.profile = { ...profile, aliases: parseAliases(aliases) };
        }
        if (kind === 'style') fields.is_active = active;
      }
      await onSave(kind, { title, ...(item && kind === 'style' && 'config' in fields ? {} : { content }), revision,
        ...(item ? { is_pinned: pinned, fields } : {}) });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
      if (e instanceof ApiError && e.current)
        setConflict(e.current as LibraryItem);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={item ? "编辑素材" : "新建素材"} onClose={() => { if (!busy) onClose(); }}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
        className="sheet-form"
      >
        {!item && (
          <label>
            素材分类
            <select
              aria-label="素材分类"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              {Object.entries(categoryNames)
                .filter(
                  ([k]) =>
                    ![
                      "summary",
                      "asset",
                      "style_rule",
                      "manuscript",
                      "relation",
                    ].includes(k),
                )
                .map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
            </select>
          </label>
        )}
        <label>
          素材标题
          <input
            aria-label="素材标题"
            disabled={busy}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            maxLength={240}
          />
        </label>
        {!(item && kind === 'style') && <label>
          设定内容
          <textarea
            aria-label="设定内容"
            disabled={busy}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={10}
          />
        </label>}
        {item && kind === 'entity' && <label>
          别名与称呼
          <textarea aria-label="别名与称呼" value={aliases} disabled={busy} rows={3}
            placeholder="每行一个，例如昵称、旧名或尊称"
            onChange={event => changeAliases(event.target.value)} />
          <span className="subtle">每行一个，也可用逗号分隔。Wiki 和参考检索会使用这些别名。</span>
        </label>}
        {item && <label><input type="checkbox" checked={pinned} disabled={busy}
          onChange={e => setPinned(e.target.checked)} />固定为重点参考</label>}
        {item && kind === 'style' && <label><input type="checkbox" checked={active} disabled={busy}
          onChange={e => setActive(e.target.checked)} />启用风格方案</label>}
        {item && typedFields[kind] && <details>
          <summary>类型字段（高级）</summary>
          <p className="subtle">{fieldHints[kind]} 引用必须属于本小说；空引用使用 null。只修改需要调整的字段。</p>
          <label>类型字段 JSON<textarea aria-label="类型字段 JSON" value={fieldsText} disabled={busy}
            rows={9} onChange={e => changeFields(e.target.value)} /></label>
        </details>}
        {conflicts.filter(issue => item && issue.ids.includes(item.id)).map((issue, index) =>
          <p role="status" className="error-note" key={`${issue.code}:${index}`}>{issue.message}</p>)}
        {conflictsLoading && <p role="status">正在检查素材冲突…</p>}
        {conflictsError && <p role="alert">素材冲突未能加载：{conflictsError}<button type="button" onClick={onRetryConflicts}>重试素材冲突</button></p>}
        <p className="subtle">
          仅用于当前小说。保存后立即更新本项目的参考检索。
        </p>
        {error && (
          <p role="alert" className="error-note">
            {error}
          </p>
        )}
        {conflict && (
          <div className="conflict-box">
            <p>此素材已在其他窗口更新。你的输入仍保留。</p>
            <blockquote>{conflict.content}</blockquote>
            <button
              type="button"
              onClick={() => {
                setTitle(conflict.title);
                setContent(conflict.content);
                setRevision(conflict.revision);
                setFieldsText(initialFields(conflict));
                setAliases(profileAliases(conflict.record.profile));
                setAliasesChanged(false);
                setActive(conflict.record.is_active === true);
                setPinned(conflict.is_pinned);
                setConflict(null);
                setError("");
              }}
            >
              使用服务器版本
            </button>
            <button
              type="button"
              onClick={() => {
                setRevision(conflict.revision);
                setConflict(null);
                setError("已使用最新修订号，再次保存以覆盖。");
              }}
            >
              保留本地修改，准备覆盖
            </button>
          </div>
        )}
        <footer className="sheet-footer">
          {item && (
            <button
              type="button"
              className="danger-text"
              disabled={busy}
              onClick={() => setConfirm(true)}
            >
              移到回收站
            </button>
          )}
          <button
            type="submit"
            className="primary-action"
            disabled={busy || !title.trim()}
          >
            {busy ? "保存中…" : "保存素材"}
          </button>
        </footer>
        {confirm && (
          <div className="delete-confirm">
            <p>
              素材会立即退出检索，保留 30
              天后清理。关联的确认设定不会被自动改写。
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onDelete();
                  onClose();
                } catch (e) {
                  setError(String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              确认移到回收站
            </button>
            <button type="button" onClick={() => setConfirm(false)}>
              取消
            </button>
          </div>
        )}
      </form>
    </Modal>
  );
}
