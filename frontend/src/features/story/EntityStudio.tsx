import { useState } from "react";
import { useGuardedCreate } from '../../lib/useGuardedCreate';

import type { StoryEntity } from "../../lib/types";

interface EntityStudioProps {
  entities: StoryEntity[];
  onCreate: (entity: Omit<StoryEntity, "id">) => Promise<void>;
}

export function EntityStudio({ entities, onCreate }: EntityStudioProps) {
  const [kind, setKind] = useState<StoryEntity["kind"]>("character");
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  const [voice, setVoice] = useState("");

  const creation = useGuardedCreate(JSON.stringify([kind, name, summary, voice]), () => onCreate({
      kind,
      name,
      summary,
      profile: { voice },
      state: kind === "character" ? { alive: true } : {},
    }), () => {
    setName("");
    setSummary("");
    setVoice("");
  });

  const kindLabel = {
    character: "人物",
    location: "地点",
    organization: "组织",
    item: "物件",
  }[kind];

  return (
    <section className="studio-section">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">人物档案</span>
          <h2>让人物拥有自己的重力</h2>
        </div>
      </header>
      <div className="entity-list">
        {entities.map((entity) => (
          <article key={entity.id} className="entity-card">
            <span>{entity.kind}</span>
            <h3>{entity.name}</h3>
            <p>{entity.summary}</p>
          </article>
        ))}
      </div>
      <form className="entity-form" onSubmit={creation.submit}>
        <label>
          实体类型
          <select
            aria-label="实体类型"
            value={kind}
            onChange={(event) =>
              setKind(event.target.value as StoryEntity["kind"])
            }
          >
            <option value="character">人物</option>
            <option value="location">地点</option>
            <option value="organization">组织</option>
            <option value="item">物件</option>
          </select>
        </label>
        <label>
          姓名或名称
          <input
            aria-label="姓名或名称"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
          />
        </label>
        <label>
          人物简介
          <textarea
            aria-label="人物简介"
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
          />
        </label>
        <label>
          声音特征
          <textarea
            aria-label="声音特征"
            value={voice}
            onChange={(event) => setVoice(event.target.value)}
          />
        </label>
        <button type="submit" disabled={creation.busy}>保存{kindLabel}</button>
        {creation.error && <p role="alert">{creation.error}</p>}
      </form>
    </section>
  );
}
