import { useState } from "react";
import { useGuardedCreate } from '../../lib/useGuardedCreate';

export interface Idea {
  id: string;
  title: string;
  content: string;
  tags: string[];
  source: string;
  status: string;
}

interface IdeaBoardProps {
  ideas: Idea[];
  onCreate: (idea: Omit<Idea, "id">) => Promise<void>;
}

function splitList(value: string) {
  return value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function IdeaBoard({ ideas, onCreate }: IdeaBoardProps) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [source, setSource] = useState("");

  const creation = useGuardedCreate(JSON.stringify([title, content, tags, source]), () => onCreate({
      title,
      content,
      tags: splitList(tags),
      source,
      status: "captured",
    }), () => {
    setTitle("");
    setContent("");
    setTags("");
    setSource("");
  });

  return (
    <section className="studio-section">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">灵感匣</span>
          <h2>先捕捉火花，再决定它属于哪里</h2>
        </div>
        <p>碎片不会自动进入小说圣经，避免未经确认的想法污染事实。</p>
      </header>
      <div className="idea-grid">
        {ideas.map((idea) => (
          <article key={idea.id} className="idea-card">
            <small>{idea.source || "作者灵感"}</small>
            <h3>{idea.title}</h3>
            <p>{idea.content}</p>
            <div>
              {idea.tags.map((tag) => (
                <span key={tag}>#{tag}</span>
              ))}
            </div>
          </article>
        ))}
        {!ideas.length && (
          <p className="empty-note">
            这里还很安静。写下一个画面、一句对白或一个“如果”。
          </p>
        )}
      </div>
      <form className="knowledge-form" onSubmit={creation.submit}>
        <label>
          灵感标题
          <input
            aria-label="灵感标题"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
        </label>
        <label className="span-two">
          灵感内容
          <textarea
            aria-label="灵感内容"
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        </label>
        <label>
          标签
          <input
            aria-label="标签"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="人物, 世界观, 钩子"
          />
        </label>
        <label>
          来源
          <input
            aria-label="来源"
            value={source}
            onChange={(event) => setSource(event.target.value)}
            placeholder="梦境、读书笔记、随记"
          />
        </label>
        <button type="submit" disabled={creation.busy}>收进灵感匣</button>
        {creation.error && <p role="alert">{creation.error}</p>}
      </form>
    </section>
  );
}
