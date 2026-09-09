import { useState, type FormEvent } from "react";

interface Asset {
  project_id?: string;
  id: string;
  kind: string;
  prompt: string;
  relative_path: string;
  status: string;
  provider: string;
  model: string;
}

export function AssetLibrary({
  assets,
  onGenerate,
}: {
  assets: Asset[];
  onGenerate: (request: {
    kind: "character" | "scene";
    prompt: string;
    size: string;
  }) => Promise<void>;
}) {
  const [kind, setKind] = useState<"character" | "scene">("character");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onGenerate({ kind, prompt, size: "1024x1024" });
      setPrompt("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="asset-library">
      <header role="group" className="section-heading">
        <div>
          <span className="eyebrow">视觉素材库</span>
          <h2>给人物与地点一张可回看的脸</h2>
        </div>
      </header>
      <form className="asset-generator" onSubmit={submit}>
        <label>
          素材类型
          <select
            aria-label="素材类型"
            value={kind}
            onChange={(event) =>
              setKind(event.target.value as "character" | "scene")
            }
          >
            <option value="character">角色形象</option>
            <option value="scene">场景概念</option>
          </select>
        </label>
        <label>
          画面要求
          <textarea
            aria-label="画面要求"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? "正在生成…" : "生成概念图"}
        </button>
        {error && <p role="alert">{error}</p>}
      </form>
      <div className="asset-grid">
        {assets.map((asset) => (
          <article key={asset.id} className="asset-card">
            <img
              className="asset-image"
              src={`/api/v1/projects/${asset.project_id}/assets/${asset.id}/file`}
              alt={asset.prompt}
              loading="lazy"
            />
            <strong>{asset.prompt}</strong>
            <small>
              {asset.provider} · {asset.model} · {asset.status}
            </small>
          </article>
        ))}
      </div>
    </section>
  );
}
