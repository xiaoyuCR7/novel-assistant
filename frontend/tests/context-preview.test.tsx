import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ContextPreview } from "../src/features/ai/ContextPreview";
import { ChatWorkspace } from "../src/features/ai/ChatWorkspace";

describe("context preview truncation", () => {
  it("shows top-level omitted source ids and the total omitted count", () => {
    render(
      <ContextPreview
        context={{
          fragments: [],
          dropped_source_ids: ["canon:secret/1", "chapter:older&2"],
        }}
      />,
    );

    expect(screen.getByText("上下文已截断", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("为适应预算省略 2 项", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("canon:secret/1")).toBeInTheDocument();
    expect(screen.getByText("chapter:older&2")).toBeInTheDocument();
  });

  it("does not show a truncation warning for old or complete snapshots", () => {
    render(<ContextPreview context={{ fragments: [] }} />);

    expect(screen.queryByText("上下文已截断", { exact: false })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("上下文尚未组装");
  });

  it("shows token usage only when both estimates are numeric", () => {
    const { rerender } = render(
      <ContextPreview context={{ fragments: [], total_estimated_tokens: 42 }} />,
    );

    expect(screen.queryByText("tokens", { exact: false })).not.toBeInTheDocument();

    rerender(
      <ContextPreview context={{ fragments: [], total_estimated_tokens: 42, token_budget: 100 }} />,
    );
    expect(screen.getByText("42 / 100 tokens")).toBeInTheDocument();
  });

  it("keeps stage-specific dropped-source reporting", () => {
    render(
      <ContextPreview
        context={{
          fragments: [],
          stage_inputs: {
            drafting: {
              estimated_input_tokens: 42,
              included_sources: [{ source_type: "canon", source_id: "kept" }],
              dropped_source_ids: ["stage-drop"],
            },
          },
        }}
      />,
    );

    expect(screen.getByText("各阶段实际输入（估算）")).toBeInTheDocument();
    expect(screen.getByText("为预算省略 1 项软参考", { exact: false })).toBeInTheDocument();
  });

  it("keeps truncation visible in chat when every candidate was omitted", () => {
    const job = {
      id: "truncated-job",
      status: "succeeded",
      task_type: "chat",
      control_revision: 1,
    };
    render(
      <ChatWorkspace
        chapterTitle="Chapter"
        hasChapter
        jobs={[job]}
        selectedJobId={job.id}
        selectedJob={{
          ...job,
          context_snapshot: { fragments: [], dropped_source_ids: ["omitted-only"] },
          result: { reply: "done" },
        }}
        running={false}
        onSend={async () => {}}
        onAccept={async () => {}}
        onOpenManuscript={() => {}}
      />,
    );

    expect(screen.getByText("上下文已截断", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("omitted-only")).toBeInTheDocument();
  });
});
