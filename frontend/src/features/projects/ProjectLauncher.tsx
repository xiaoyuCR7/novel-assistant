import { useState, type FormEvent } from "react";

interface ProjectDraft {
  title: string;
  premise: string;
  genre: string;
  target_words: number;
  daily_goal: number;
}

interface ProjectLauncherProps {
  onCreate: (draft: ProjectDraft) => Promise<void>;
  onImport?: () => void;
  busy: boolean;
}

export function ProjectLauncher({ onCreate, onImport, busy }: ProjectLauncherProps) {
  const [title, setTitle] = useState("");
  const [premise, setPremise] = useState("");
  const [genre, setGenre] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    await onCreate({
      title,
      premise,
      genre,
      target_words: 200_000,
      daily_goal: 1_500,
    });
  }

  return (
    <main className="launcher-page">
      <section className="launcher-intro">
        <span className="eyebrow">从一个不可忘记的念头开始</span>
        <h1>
          把故事交给结构，
          <br />
          把声音留给你。
        </h1>
        <p>
          建立小说圣经、章节契约和可追溯版本，让长篇在数十万字后仍记得最初的承诺。
        </p>
      </section>
      <form className="launcher-form" onSubmit={submit}>
        <label>
          小说名称
          <input
            aria-label="小说名称"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
        </label>
        <label>
          核心前提
          <textarea
            aria-label="核心前提"
            value={premise}
            onChange={(event) => setPremise(event.target.value)}
            required
          />
        </label>
        <label>
          类型
          <input
            aria-label="类型"
            value={genre}
            onChange={(event) => setGenre(event.target.value)}
          />
        </label>
        <div className="launcher-project-actions">
          <button className="primary-action" type="submit" disabled={busy}>
            {busy ? "正在建立…" : "新建空白小说"}
          </button>
          {onImport && (
            <button className="secondary-action" type="button" disabled={busy} onClick={onImport}>
              导入已有小说
            </button>
          )}
        </div>
      </form>
    </main>
  );
}
