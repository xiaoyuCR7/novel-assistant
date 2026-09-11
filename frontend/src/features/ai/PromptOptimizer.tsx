import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Modal } from '../../components/Modal';
import {
  analyzePrompt, buildOptimizedPrompt, PROMPT_MAX_LENGTH, promptTemplates,
  type PromptAnswers, type PromptFieldId, type PromptTemplateId,
} from './promptOptimizerEngine';
import './prompt-optimizer.css';

type Editor = { source: string; expected: string; template: PromptTemplateId; answers: PromptAnswers };
type EditHistory = { before: string; after: string; position: 'before' | 'after';
  beforeEditor: Editor; afterEditor: Editor };
type Props = { value: string; task: string; hasChapter: boolean; chapterTitle: string;
  disabled?: boolean; onReplace: (expected: string, replacement: string) => boolean };
const emptyAnswers = (): PromptAnswers => ({ goal: '', scope: '', constraints: '', output: '' });
const fields: Array<{ id: PromptFieldId; label: string; question: string; placeholder: string }> = [
  { id: 'goal', label: '具体目标', question: '你希望具体改善什么？', placeholder: '例如：删去重复比喻，让动作更清楚' },
  { id: 'scope', label: '处理范围', question: '要处理哪一章、哪段内容或哪个问题？', placeholder: '例如：第二章结尾两段' },
  { id: 'constraints', label: '保留与限制', question: '哪些内容要保留，哪些改动不能做？', placeholder: '可选：保留人物动机，不新增事件' },
  { id: 'output', label: '交付形式', question: '你希望收到怎样的结果？', placeholder: '可选：修改后的正文，再附简短说明' },
];
const taskTemplates: Record<string, PromptTemplateId> = { rewrite: 'polish', shorten: 'polish',
  continue: 'continue', full_chapter: 'continue', plan: 'plan', review: 'review', suggest: 'brainstorm' };
const conflictMessage = '输入已更新，未覆盖新内容。请按最新输入重新整理。';

export function PromptOptimizer({ value, task, hasChapter, chapterTitle, disabled = false, onReplace }: Props) {
  const [open, setOpen] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [history, setHistory] = useState<EditHistory | null>(null);
  const [error, setError] = useState('');
  const previousValue = useRef(value);
  const id = useId();
  const analysis = useMemo(() => analyzePrompt(value), [value]);
  const source = editor?.source ?? value;
  const sourceAnalysis = useMemo(() => analyzePrompt(source), [source]);
  const optimized = useMemo(() => editor
    ? buildOptimizedPrompt(editor.source, editor.template, editor.answers) : '', [editor]);
  const template = promptTemplates.find(item => item.id === editor?.template) ?? promptTemplates[0];
  const missing = sourceAnalysis.missing.filter(field => (field === 'goal' || field === 'scope')
    && !editor?.answers[field].trim());
  const changed = !!editor && editor.expected !== value;
  const tooLong = optimized.length > PROMPT_MAX_LENGTH;
  const canApply = !!editor && !!source.trim() && !!optimized.trim() && !sourceAnalysis.tooLong
    && !tooLong && !missing.length && !changed && !disabled && optimized !== value;
  const historyMatches = !!history && value === history[history.position];

  useEffect(() => {
    // Sending or clearing an input ends this edit history; an old message cannot be revived.
    if (!value.trim()) {
      setHistory(null);
      if (previousValue.current.trim()) { setEditor(null); setOpen(false); setError(''); }
    }
    previousValue.current = value;
  }, [value]);

  function startFresh() {
    setEditor({ source: value, expected: value, template: analysis.suggestedTemplate === 'custom'
      ? taskTemplates[task] ?? 'custom' : analysis.suggestedTemplate, answers: emptyAnswers() });
    setError('');
  }
  function show() {
    if (!editor || editor.expected !== value) startFresh();
    setError(''); setOpen(true);
  }
  function answer(field: PromptFieldId, text: string) {
    setEditor(current => current ? { ...current, answers: { ...current.answers, [field]: text } } : current);
  }
  function suggest(field: PromptFieldId, text: string) {
    setEditor(current => {
      if (!current || current.answers[field].split('\n').includes(text)) return current;
      const previous = current.answers[field];
      return { ...current, answers: { ...current.answers, [field]: previous ? `${previous}\n${text}` : text } };
    });
  }
  function apply() {
    if (!canApply || !editor) return;
    if (!onReplace(editor.expected, optimized)) { setError(conflictMessage); return; }
    const afterEditor = { ...editor, expected: optimized };
    // Revisions keep the original source; undo restores both the last input and its editable fields.
    const beforeEditor = history && historyMatches ? history[`${history.position}Editor`] : editor;
    setHistory({ before: editor.expected, after: optimized, position: 'after', beforeEditor, afterEditor });
    setEditor(afterEditor);
    setError(''); setOpen(false);
  }
  function travel() {
    if (!history || !historyMatches || disabled) return;
    const destination = history.position === 'after' ? 'before' : 'after';
    if (!onReplace(history[history.position], history[destination])) { setError(conflictMessage); return; }
    setEditor(history[`${destination}Editor`]);
    setHistory({ ...history, position: destination }); setError('');
  }

  return <div className="prompt-optimizer">
    <div className="prompt-optimizer-actions">
      <button type="button" onClick={show}>完善提示词</button>
      {history && <button type="button" disabled={disabled || !historyMatches} onClick={travel}>
        {history.position === 'after' ? '撤销提示词优化' : '恢复提示词优化'}
      </button>}
    </div>
    {!!analysis.phrases.length && !!analysis.missing.length && <p className="prompt-optimizer-hint">这些表达可以更具体：{analysis.phrases.join('、')}。</p>}
    {history && !historyMatches && <p className="prompt-optimizer-hint">输入已手动修改，撤销与恢复不会覆盖新内容。</p>}
    {!open && error && <p role="alert" className="prompt-optimizer-error">{error}</p>}
    {open && editor && <Modal title="完善提示词" onClose={() => setOpen(false)}>
      <div className="prompt-optimizer-content">
        <p className="prompt-optimizer-intro">在本地整理你的想法，不调用模型。应用只更新输入框，不会发送消息。</p>
        <div className="prompt-optimizer-template">
          <label htmlFor={`${id}-template`}>提示词模板</label>
          <select id={`${id}-template`} value={editor.template}
            onChange={event => setEditor({ ...editor, template: event.target.value as PromptTemplateId })}>
            {promptTemplates.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          <p>{template.description} 切换模板不会替你填写答案。</p>
        </div>
        <div className="prompt-optimizer-fields">
          {fields.map(field => <div className="prompt-optimizer-field" key={field.id}>
            <label htmlFor={`${id}-${field.id}`}>{field.label}</label>
            <p id={`${id}-${field.id}-question`}>{field.question}</p>
            <textarea id={`${id}-${field.id}`} rows={2}
              aria-describedby={`${id}-${field.id}-question${missing.includes(field.id) ? ` ${id}-${field.id}-required` : ''}`}
              aria-required={missing.includes(field.id) || undefined} value={editor.answers[field.id]}
              placeholder={field.placeholder} onChange={event => answer(field.id, event.target.value)} />
            <div className="prompt-optimizer-suggestions" role="group" aria-label={`${field.label}建议`}>
              {template.suggestions[field.id].map(suggestion => <button key={suggestion} type="button"
                onClick={() => suggest(field.id, suggestion)}>{suggestion}</button>)}
              {field.id === 'scope' && hasChapter && <button type="button" onClick={() => answer('scope', chapterTitle)}>使用当前章节</button>}
            </div>
            {missing.includes(field.id) && <small id={`${id}-${field.id}-required`} className="prompt-optimizer-required">请补充{field.label}；原文已明确的内容不必重复填写。</small>}
          </div>)}
        </div>
        <div className="prompt-optimizer-comparison">
          <section aria-label="原始想法"><h3>原始想法</h3><pre tabIndex={0}>{editor.source || '尚未输入想法'}</pre></section>
          <section aria-label="优化后的提示词"><h3>优化后的提示词</h3><pre tabIndex={0}>{optimized || '补充信息后，在这里查看整理结果。'}</pre></section>
        </div>
        <div className="prompt-optimizer-review">
          <p>原始想法保持原样，模板说明与补充内容会附在后面。请核对后再应用。</p>
          <p>{optimized.length.toLocaleString()} / {PROMPT_MAX_LENGTH.toLocaleString()} 字符</p>
          {!source.trim() && <p className="prompt-optimizer-error">请先在输入框写下你的想法，再进行整理。</p>}
          {sourceAnalysis.tooLong && <p className="prompt-optimizer-error">原文超过 16,000 字符，请先缩短输入；不会自动截断原文。</p>}
          {tooLong && <p className="prompt-optimizer-error">优化后超过 16,000 字符，请减少补充内容；不会自动截断。</p>}
          {disabled && <p>当前不能修改输入，可以继续阅读和整理，稍后再应用。</p>}
          {(changed || error) && <div className="prompt-optimizer-conflict">
            <p role="alert" className="prompt-optimizer-error">{error || conflictMessage}</p>
            <button type="button" onClick={startFresh}>按最新输入重新开始</button>
          </div>}
        </div>
        <footer className="prompt-optimizer-footer">
          <small>撤销与恢复仅保留本次输入的一次修改。</small>
          <div><button type="button" onClick={() => setOpen(false)}>关闭</button>
            <button type="button" className="primary-action" disabled={!canApply} onClick={apply}>应用到输入框</button></div>
        </footer>
      </div>
    </Modal>}
  </div>;
}
