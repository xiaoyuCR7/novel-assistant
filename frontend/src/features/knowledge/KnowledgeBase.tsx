import { useState } from "react";
import { useGuardedCreate } from '../../lib/useGuardedCreate';

export interface CanonFact {
  id: string;
  predicate: string;
  value: unknown;
  source_note: string;
  status: string;
}

export interface TimelineEvent {
  id: string;
  title: string;
  story_time: string;
  sort_key: number;
  description: string;
}

interface KnowledgeBaseProps {
  canonFacts: CanonFact[];
  timeline: TimelineEvent[];
  onCreateCanon: (fact: object) => Promise<void>;
  onCreateTimeline: (event: object) => Promise<void>;
}

export function KnowledgeBase({
  canonFacts,
  timeline,
  onCreateCanon,
  onCreateTimeline,
}: KnowledgeBaseProps) {
  const [predicate, setPredicate] = useState("");
  const [value, setValue] = useState("");
  const [sourceNote, setSourceNote] = useState("");
  const [eventTitle, setEventTitle] = useState("");
  const [storyTime, setStoryTime] = useState("");
  const [description, setDescription] = useState("");

  const factCreation = useGuardedCreate(JSON.stringify([predicate, value, sourceNote]), () => onCreateCanon({
      predicate,
      value,
      source_note: sourceNote,
      status: "confirmed",
    }), () => {
    setPredicate("");
    setValue("");
    setSourceNote("");
  });

  const eventCreation = useGuardedCreate(JSON.stringify([eventTitle, storyTime, description]), () => onCreateTimeline({
      title: eventTitle,
      story_time: storyTime,
      description,
      sort_key: timeline.length + 1,
    }), () => {
    setEventTitle("");
    setStoryTime("");
    setDescription("");
  });

  return (
    <section className="studio-section">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">小说圣经</span>
          <h2>把确定的事实与时间固定下来</h2>
        </div>
        <p>AI 生成时会优先遵守这里的确认内容。</p>
      </header>
      <div className="knowledge-columns">
        <article className="knowledge-panel">
          <h3>确认事实</h3>
          {canonFacts.map((fact) => (
            <div className="fact-row" key={fact.id}>
              <strong>{fact.predicate}</strong>
              <span>{String(fact.value)}</span>
              <small>{fact.status}</small>
            </div>
          ))}
          <form onSubmit={factCreation.submit}>
            <label>
              事实关系
              <input
                aria-label="事实关系"
                value={predicate}
                onChange={(event) => setPredicate(event.target.value)}
                placeholder="身份 / 惧怕 / 拥有"
                required
              />
            </label>
            <label>
              事实内容
              <input
                aria-label="事实内容"
                value={value}
                onChange={(event) => setValue(event.target.value)}
                required
              />
            </label>
            <label>
              证据或来源
              <input
                aria-label="证据或来源"
                value={sourceNote}
                onChange={(event) => setSourceNote(event.target.value)}
              />
            </label>
            <button type="submit" disabled={factCreation.busy}>确认事实</button>
            {factCreation.error && <p role="alert">{factCreation.error}</p>}
          </form>
        </article>
        <article className="knowledge-panel">
          <h3>故事时间线</h3>
          {timeline.map((item) => (
            <div className="timeline-row" key={item.id}>
              <small>{item.story_time || "时间待定"}</small>
              <strong>{item.title}</strong>
              <p>{item.description}</p>
            </div>
          ))}
          <form onSubmit={eventCreation.submit}>
            <label>
              事件标题
              <input
                aria-label="事件标题"
                value={eventTitle}
                onChange={(event) => setEventTitle(event.target.value)}
                required
              />
            </label>
            <label>
              故事时间
              <input
                aria-label="故事时间"
                value={storyTime}
                onChange={(event) => setStoryTime(event.target.value)}
              />
            </label>
            <label>
              事件说明
              <textarea
                aria-label="事件说明"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <button type="submit" disabled={eventCreation.busy}>加入时间线</button>
            {eventCreation.error && <p role="alert">{eventCreation.error}</p>}
          </form>
        </article>
      </div>
    </section>
  );
}
