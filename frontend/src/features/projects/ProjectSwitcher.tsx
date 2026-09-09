import { Icon } from "../../components/Icon";
export function ProjectSwitcher({
  projects,
  activeId,
  onChange,
  onCreate,
}: {
  projects: Array<{ id: string; title: string }>;
  activeId: string;
  onChange: (id: string) => void;
  onCreate: () => void;
}) {
  return (
    <div className="project-switcher">
      <span className="project-emblem">
        <Icon name="book" />
      </span>
      <div>
        <span className="utility-label">我的小说 · 独立项目</span>
        <select
          aria-label="当前小说项目"
          value={activeId}
          onChange={(e) => onChange(e.target.value)}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title}
            </option>
          ))}
        </select>
      </div>
      <button
        className="icon-button"
        aria-label="创建新小说"
        onClick={onCreate}
      >
        <Icon name="plus" size={17} />
      </button>
    </div>
  );
}
