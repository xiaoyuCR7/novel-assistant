import { useState, type FormEvent } from "react";
import type { MemoryConflict, StyleProfile } from '../../lib/types';

interface Candidate {
  id: string;
  instruction: string;
  category: string;
  status: string;
  requires_instruction?: boolean;
}
interface Rule {
  id: string;
  instruction: string;
  status: string;
}

interface StyleLabProps {
  candidates: Candidate[];
  rules: Rule[];
  profiles?: StyleProfile[];
  conflicts?: MemoryConflict[];
  onUpdateProfile?: (profile: StyleProfile, isActive: boolean) => Promise<void>;
  onPinProfile?: (profile: StyleProfile, isPinned: boolean) => Promise<void>;
  onConfirm: (id: string, instruction?: string) => Promise<void>;
  onDisable: (id: string) => Promise<void>;
  onCreateProfile?: (profile: {
    name: string;
    is_active: boolean;
    config: Record<string, unknown>;
  }) => Promise<void>;
}

function splitList(value: string) {
  return value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function StyleLab({
  candidates,
  rules,
  profiles = [],
  onConfirm,
  onDisable,
  onCreateProfile,
  onUpdateProfile,
  onPinProfile,
  conflicts = [],
}: StyleLabProps) {
  const [name, setName] = useState("");
  const [distance, setDistance] = useState("close");
  const [rhythm, setRhythm] = useState("balanced");
  const [forbidden, setForbidden] = useState("");
  const [instructions, setInstructions] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError('');
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!onCreateProfile) return;
    await act(async () => {
      await onCreateProfile({
      name,
      is_active: true,
      config: { distance, rhythm, forbidden_phrases: splitList(forbidden) },
    });
    setName("");
    setForbidden("");
    });
  }

  return (
    <section className="style-lab">
      <header role="group">
        <span className="eyebrow">风格实验室</span>
        <h3>偏好必须经过你的确认</h3>
      </header>
      <p className="subtle">必遵守：已启用并固定，每次作为约束带入。按需参考：已启用但未固定，仅在检索选中或明确引用时采用。停用方案不会生效。</p>
      {error && <p role="alert" className="error-note">{error}</p>}
      {conflicts.filter(issue => issue.code === 'STYLE_CONFLICT').map((issue, index) =>
        <p role="status" className="error-note" key={index}>{issue.message}</p>)}
      {onCreateProfile && (
        <form className="style-profile-form" onSubmit={submit}>
          <label>
            风格方案名
            <input
              aria-label="风格方案名"
              disabled={busy}
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </label>
          <label>
            叙事距离
            <select
              aria-label="叙事距离"
              disabled={busy}
              value={distance}
              onChange={(event) => setDistance(event.target.value)}
            >
              <option value="close">贴近人物</option>
              <option value="medium">中距离</option>
              <option value="omniscient">全知</option>
            </select>
          </label>
          <label>
            句式节奏
            <select
              aria-label="句式节奏"
              disabled={busy}
              value={rhythm}
              onChange={(event) => setRhythm(event.target.value)}
            >
              <option value="balanced">舒展均衡</option>
              <option value="crisp">短促利落</option>
              <option value="lyrical">绵密抒情</option>
            </select>
          </label>
          <label>
            禁用表达
            <input
              aria-label="禁用表达"
              disabled={busy}
              value={forbidden}
              onChange={(event) => setForbidden(event.target.value)}
              placeholder="逗号分隔"
            />
          </label>
          <button type="submit" disabled={busy}>保存并启用方案</button>
        </form>
      )}
      <div className="preference-list">
        {profiles.map((profile) => (
          <article key={profile.id}>
              <small>{!profile.is_active ? '已停用' : profile.is_pinned ? '必遵守' : '按需参考'}</small>
              <p>{profile.name}</p>
              {onPinProfile && <button type="button" disabled={busy || !profile.is_active}
                aria-label={`${profile.is_pinned ? '取消固定' : '固定'}方案 ${profile.name}`}
                onClick={() => void act(() => onPinProfile(profile, !profile.is_pinned))}>
                {profile.is_pinned ? '改为按需参考' : '设为必遵守'}</button>}
            {onUpdateProfile && <button type="button" disabled={busy}
              aria-label={`${profile.is_active ? '停用' : '启用'}方案 ${profile.name}`}
              onClick={() => void act(() => onUpdateProfile(profile, !profile.is_active))}>
              {profile.is_active ? '停用方案' : '启用方案'}
            </button>}
          </article>
        ))}
        {candidates.map((candidate) => {
          const needsInstruction = candidate.requires_instruction || !candidate.instruction.trim();
          const instruction = (instructions[candidate.id] ?? '').trim();
          return (
          <article key={candidate.id}>
            <small>
              {candidate.category} · {needsInstruction ? '待补充偏好（未生效）' : candidate.status}
            </small>
            {needsInstruction ? <label>
              补充风格偏好
              <textarea aria-label="补充风格偏好" maxLength={2000} disabled={busy}
                value={instructions[candidate.id] ?? ''}
                placeholder="例如：动作已经表达情绪时，不再追加解释。"
                onChange={event => setInstructions(current => ({
                  ...current, [candidate.id]: event.target.value,
                }))} />
            </label> : <p>{candidate.instruction}</p>}
            <div>
              <button type="button" disabled={busy || (needsInstruction && !instruction)}
                onClick={() => void act(() => needsInstruction
                  ? onConfirm(candidate.id, instruction) : onConfirm(candidate.id))}>
                确认风格规则
              </button>
              <button type="button" disabled={busy} onClick={() => void act(() => onDisable(candidate.id))}>
                停用风格规则
              </button>
            </div>
          </article>
        ); })}
        {rules.map((rule) => (
          <article key={rule.id}>
            <small>{rule.status}</small>
            <p>{rule.instruction}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
