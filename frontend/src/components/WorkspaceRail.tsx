import { Icon } from "./Icon";
export const views = [
  ["write", "章节写作", "write"],
  ["quality", "质量优化", "spark"],
  ["library", "素材库", "book"],
  ["wiki", "小说 Wiki", "book"],
  ["ideas", "灵感匣", "idea"],
  ["story", "故事地图", "map"],
  ["entities", "人物世界", "people"],
  ["knowledge", "小说圣经", "shield"],
  ["preparation", "创作准备", "idea"],
  ["ai", "AI 导演", "spark"],
  ["conflicts", "冲突中心", "shield"],
  ["style", "风格实验室", "tune"],
  ["assets", "视觉素材", "image"],
  ["settings", "设置", "tune"],
] as const;
export type View = (typeof views)[number][0];
export function WorkspaceRail({
  view,
  onChange,
}: {
  view: View;
  onChange: (view: View) => void;
}) {
  return (
    <nav className="focus-navigation" aria-label="创作空间">
      {(
        [
          ["ai", "创作", "write"],
          ["library", "资料库", "book"],
          ["settings", "设置", "tune"],
        ] as const
      ).map(([id, label, icon]) => (
        <button
          key={id}
          title={label}
          aria-label={label}
          aria-current={
            (
              id === "ai"
                ? ["ai", "write", "quality", "preparation"].includes(view)
                : id === "library"
                  ? !["ai", "write", "quality", "preparation", "settings"].includes(view)
                  : view === id
            )
              ? "page"
              : undefined
          }
          onClick={() => onChange(id)}
        >
          <Icon name={icon} />
          <span>{label}</span>
        </button>
      ))}
    </nav>
  );
}
