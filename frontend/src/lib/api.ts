import type {
  LibraryItem,
  LibraryPage,
  LibrarySummary,
  MaterialLifecycleResult,
  MaterialReference,
  MemoryConflict,
  MemoryCandidateMutationResult,
  MemoryCandidatePatchRequest,
  MemoryCandidatePage,
  MemoryCandidateStatus,
  ImportAnalysis,
  ImportAnalysisAction,
  ImportCommitResponse,
  ImportDraft,
  ImportDraftPatch,
  ImportLimits,
  ImportMemoryCandidate,
  MemoryCandidateBulkConfirmEntry,
  ChapterSummary,
  CanonFact,
  ChapterDocument,
  ChapterVersion,
  Idea,
  Project,
  ProjectPreparationState,
  PreparationQuestionCommand,
  RagHealth,
  StoryEntity,
  StoryNode,
  StyleProfile,
  TimelineEvent,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public current?: unknown,
    public code?: string,
    public jobId?: string,
  ) {
    super(message);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function apiError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = undefined;
    }
  }
  const body = isRecord(payload) && "detail" in payload ? payload.detail : payload;
  const detail = isRecord(body) ? body : undefined;
  const message = typeof body === "string"
    ? body
    : typeof detail?.message === "string"
      ? detail.message
      : typeof detail?.code === "string"
        ? detail.code
        : text || `请求失败 (${response.status})`;
  return new ApiError(
    response.status,
    message,
    detail?.current,
    typeof detail?.code === "string" ? detail.code : undefined,
    typeof detail?.job_id === "string" ? detail.job_id : undefined,
  );
}

async function responseData<T>(response: Response): Promise<T> {
  if (!response.ok) throw await apiError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

function multipartHeaders(headers: HeadersInit | undefined): HeadersInit | undefined {
  if (headers === undefined) return undefined;
  if (headers instanceof Headers) {
    const safe = new Headers(headers);
    safe.delete("Content-Type");
    return safe;
  }
  if (Array.isArray(headers)) {
    return headers.filter(([name]) => name.toLowerCase() !== "content-type");
  }
  return Object.fromEntries(
    Object.entries(headers).filter(([name]) => name.toLowerCase() !== "content-type"),
  );
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  return responseData<T>(response);
}

export async function uploadRequest<T>(
  path: string,
  body: FormData,
  init: Omit<RequestInit, "body" | "method"> = {},
): Promise<T> {
  const { headers, ...options } = init;
  const safeHeaders = multipartHeaders(headers);
  const response = await fetch(path, {
    ...options,
    ...(safeHeaders === undefined ? {} : { headers: safeHeaders }),
    method: "POST",
    body,
  });
  return responseData<T>(response);
}

export interface Workspace {
  project: Project;
  ideas: Idea[];
  nodes: StoryNode[];
  entities: StoryEntity[];
  relations: unknown[];
  canon_facts: CanonFact[];
  timeline: TimelineEvent[];
  plots: Array<{
    id: string;
    kind: "main" | "subplot" | "character" | "foreshadowing";
    title: string;
    status: string;
    start_node_id?: string | null;
    due_node_id?: string | null;
  }>;
  styles: StyleProfile[];
  memory_conflicts?: MemoryConflict[];
}

export type NavigationWorkspace = Pick<Workspace, 'project' | 'nodes'>;
export interface WorkspaceViews {
  ideas: Pick<Workspace, 'ideas'>;
  story: Pick<Workspace, 'nodes' | 'plots'>;
  entities: Pick<Workspace, 'entities' | 'relations'>;
  knowledge: Pick<Workspace, 'canon_facts' | 'timeline'>;
  style: Pick<Workspace, 'styles'> & { memory_conflicts: MemoryConflict[] };
  library: { memory_conflicts: MemoryConflict[] };
}

export interface Progress {
  current_words: number;
  target_words: number;
  completion_ratio: number;
  chapter_count: number;
  completed_chapters: number;
  daily_goal: number;
}
export interface Asset {
  id: string;
  kind: string;
  prompt: string;
  relative_path: string;
  status: string;
  provider: string;
  model: string;
}
export interface JobSummary {
  id: string;
  status: string;
  task_type: string;
  instructions?: string;
  chapter_id?: string | null;
  accepted_version_id?: string | null;
  error_message?: string;
  project_id?: string;
  control_revision?: number;
  current_stage?: string | null;
  recovery_reason?: string | null;
  allowed_actions?: string[];
  effects?: { summary_id?: string; ledger_pending?: boolean };
  created_at?: string;
  updated_at?: string;
  prompt_version?: string;
  error_code?: string | null;
  preview?: string;
  status_url?: string;
  replaces_job_id?: string | null;
  replacement_requires_confirmation?: boolean;
}
export interface JobPreview {
  job_id: string;
  status: string;
  stage: string | null;
  text: string;
  sequence: number;
  truncated: boolean;
}
export interface JobPage {
  items: JobSummary[];
  next_cursor: string | null;
}
export interface VersionSummary {
  id: string;
  chapter_id: string;
  created_at: string;
  source: string;
  summary: string;
  word_count: number;
  parent_version_id: string | null;
  restored_from_version_id: string | null;
  generation_job_id: string | null;
}
export interface VersionPage {
  items: VersionSummary[];
  next_cursor: string | null;
}
export interface AIJob extends JobSummary {
  token_budget?: number;
  context_snapshot: {
    token_budget?: number;
    total_estimated_tokens?: number;
    fragments?: Array<{
      source_type: string;
      source_id: string;
      reason: string;
      hard: boolean;
      estimated_tokens: number;
    }>;
    dropped_source_ids?: string[];
    stage_inputs?: Record<string, {
      estimated_input_tokens: number;
      included_sources: Array<{ source_type: string; source_id: string }>;
      dropped_source_ids: string[];
    }>;
  };
  result: {
    candidate_text?: string;
    stage_order?: string[];
    reply?: string;
    output?: Record<string, unknown>;
  };
}
export interface TaskBudgetPreflight {
  required_input_tokens: number;
  token_budget: number;
  output_token_budget: number;
  context_capacity: number;
  effective_input_limit: number;
  can_fit: boolean;
  estimated: true;
  scope: 'first-stage-hard-only';
  message: string;
}
export interface ModelConfig {
  mode: "demo" | "local" | "api";
  base_url: string;
  model: string;
  has_api_key: boolean;
  external_consent: boolean;
  output_token_budget?: number;
  context_capacity?: number;
  deadline_seconds?: number;
  output_parameter?: 'max_tokens' | 'max_completion_tokens';
  thinking_mode?: 'provider_default' | 'disabled' | 'enabled';
}
export interface PreferenceCandidate {
  id: string;
  instruction: string;
  category: string;
  status: string;
  requires_instruction?: boolean;
}
export interface ConflictOption {
  id: string;
  mode: string;
  title: string;
  benefit: string;
  risk: string;
  ripple_effects: string[];
}
export interface EntityStateConflictDetail {
  code: "ENTITY_STATE_CONFLICT";
  entity_id: string;
  key: string;
  state_ids: string[];
  values: unknown[];
}
export interface ConflictEntityState {
  id: string;
  entity_id: string;
  data: Record<string, unknown>;
  valid_from_node_id: string;
  valid_to_node_id?: string | null;
  status: string;
  revision: number;
}
export interface Conflict {
  id: string;
  chapter_id?: string;
  code: string;
  severity: string;
  message: string;
  status: string;
  evidence?: string[];
  options: ConflictOption[];
  entity_state_resolution?: {
    conflicts: EntityStateConflictDetail[];
    states: ConflictEntityState[];
  };
  resolved?: boolean;
}

export const api = {
  modelSettings: () => request<ModelConfig>("/api/v1/settings/model"),
  saveModelSettings: (data: object) =>
    request<ModelConfig>("/api/v1/settings/model", {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  testModel: () =>
    request<{ ok: boolean }>("/api/v1/settings/model/test", {
      method: "POST",
      body: "{}",
    }),
  health: () => request<{ ai_provider: string }>("/api/v1/health"),
  projects: () => request<Project[]>("/api/v1/projects"),
  createProject: (data: object) =>
    request<Project>("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  createImportDraft: (body: FormData, init?: Omit<RequestInit, "body" | "method">) =>
    uploadRequest<ImportDraft>("/api/v1/imports", body, init),
  importLimits: () => request<ImportLimits>("/api/v1/imports/limits"),
  getImportDraft: (id: string) => request<ImportDraft>(`/api/v1/imports/${encodeURIComponent(id)}`),
  importDraft: (id: string) => request<ImportDraft>(`/api/v1/imports/${encodeURIComponent(id)}`),
  patchImportDraft: (id: string, data: ImportDraftPatch) => request<ImportDraft>(`/api/v1/imports/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify(data),
  }),
  commitImportDraft: (id: string, expectedRevision: number) => request<ImportCommitResponse>(
    `/api/v1/imports/${encodeURIComponent(id)}/commit`,
    { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
  ),
  discardImportDraft: (id: string) => request<void>(`/api/v1/imports/${encodeURIComponent(id)}`, {
    method: "DELETE",
  }),
};

export function projectApi(projectId: string) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
  const segment = encodeURIComponent;
  const get = <T>(path: string) => request<T>(base + path);
  const send = <T>(path: string, data: object = {}, method = "POST") =>
    request<T>(base + path, { method, body: JSON.stringify(data) });
  const submit = (path: string, data: object, key: string) => request<AIJob>(base + path, {
    method: 'POST', body: JSON.stringify(data), headers: { 'Idempotency-Key': key },
  });
  return {
    preparation: () => get<ProjectPreparationState>("/preparation"),
    initializePreparation: () => send<ProjectPreparationState>("/preparation/initialize"),
    answerPreparation: (id: string, command: PreparationQuestionCommand) =>
      send<ProjectPreparationState>(`/preparation/questions/${segment(id)}`, command, "PATCH"),
    skipPreparation: (revision: number) =>
      send<ProjectPreparationState>("/preparation/skip", { revision }),
    resumePreparation: (revision: number) =>
      send<ProjectPreparationState>("/preparation/resume", { revision }),
    acknowledgePreparationImpact: (revision: number) =>
      send<ProjectPreparationState>("/preparation/impact/acknowledge", { revision }),
    analyzePreparation: (key: string) => submit("/preparation/analyze", {}, key),
    followUpPreparation: (key: string) => submit("/preparation/follow-up", {}, key),
    workspace: () => get<Workspace>("/workspace"),
    navigation: () => get<NavigationWorkspace>('/workspace/navigation'),
    workspaceView: <V extends keyof WorkspaceViews>(view: V) => get<WorkspaceViews[V]>(`/workspace/views/${view}`),
    progress: () => get<Progress>("/progress"),
    assets: () => get<Asset[]>("/assets"),
    preferences: () => get<PreferenceCandidate[]>("/preferences"),
    activeStyleRules: () => get<{ rules: Array<{ id: string; instruction: string; status: string }> }>("/styles/active-context"),
    conflicts: () => get<Conflict[]>("/conflicts?status=open"),
    latestImportAnalysis: () => get<ImportAnalysis>("/imports/latest/analysis"),
    importAnalysis: (batchId: string) => get<ImportAnalysis>(`/imports/${segment(batchId)}/analysis`),
    pauseImportAnalysis: (batchId: string, revision: number) => send<ImportAnalysis>(
      `/imports/${segment(batchId)}/analysis/pause`, { revision } satisfies ImportAnalysisAction,
    ),
    continueImportAnalysis: (batchId: string, revision: number, adoptCurrentProvider = false) => send<ImportAnalysis>(
      `/imports/${segment(batchId)}/analysis/continue`,
      { revision, adopt_current_provider: adoptCurrentProvider } satisfies ImportAnalysisAction,
    ),
    retryImportAnalysis: (batchId: string, revision: number, adoptCurrentProvider = false) => send<ImportAnalysis>(
      `/imports/${segment(batchId)}/analysis/retry`,
      { revision, adopt_current_provider: adoptCurrentProvider } satisfies ImportAnalysisAction,
    ),
    memoryCandidates: (
      cursor?: string,
      chapterId?: string,
      status: MemoryCandidateStatus | null = "pending",
      limit = 100,
      origin?: "generated" | "import",
    ) => {
      const params = new URLSearchParams({ limit: String(limit) });
      if (status !== null) params.set("status", status);
      if (origin !== undefined) params.set("origin", origin);
      if (chapterId !== undefined) params.set("chapter_id", chapterId);
      if (cursor !== undefined) params.set("cursor", cursor);
      return get<MemoryCandidatePage>(`/memory-candidates?${params.toString()}`);
    },
    editMemoryCandidate: (
      id: string,
      patch: MemoryCandidatePatchRequest,
    ) => send<MemoryCandidateMutationResult>(
      `/memory-candidates/${segment(id)}`,
      patch,
      "PATCH",
    ),
    confirmMemoryCandidate: (id: string, revision: number, resolution?: "create_separate") => send<MemoryCandidateMutationResult>(
      `/memory-candidates/${segment(id)}/confirm`,
      resolution === undefined ? { revision } : { revision, resolution },
    ),
    rejectMemoryCandidate: (id: string, revision: number) => send<MemoryCandidateMutationResult>(
      `/memory-candidates/${segment(id)}/reject`,
      { revision },
    ),
    bulkConfirmMemoryCandidates: (entries: MemoryCandidateBulkConfirmEntry[]) =>
      send<{ items: ImportMemoryCandidate[] }>("/memory-candidates/bulk-confirm", { entries }),
    createIdea: (data: object) => send<Idea>("/ideas", data),
    createNode: (data: object) => send<StoryNode>("/nodes", data),
    createEntity: (data: object) => send<StoryEntity>("/entities", data),
    createCanon: (data: object) => send<CanonFact>("/canon", data),
    createTimeline: (data: object) => send<TimelineEvent>("/timeline", data),
    createPlot: (data: object) => send("/plots", data),
    createStyle: (data: object) => send<StyleProfile>("/styles", data),
    chapter: (id: string) => get<ChapterDocument>(`/chapters/${id}`),
    saveChapter: (id: string, data: object) =>
      send<ChapterDocument>(`/chapters/${id}`, data, "PUT"),
    versions: (id: string) => get<ChapterVersion[]>(`/chapters/${segment(id)}/versions`),
    versionPage: (id: string, before?: string) => {
      const params = new URLSearchParams({ limit: "50" });
      if (before !== undefined) params.set("before", before);
      return get<VersionPage>(`/chapters/${segment(id)}/versions/page?${params.toString()}`);
    },
    compareVersions: async (id: string, from: string, to: string) =>
      (
        await get<{ diff: string }>(
          `/chapters/${segment(id)}/versions/${segment(from)}/diff/${segment(to)}`,
        )
      ).diff,
    restoreVersion: (id: string, version: string, expectedRevision: number) =>
      send<ChapterVersion>(`/chapters/${segment(id)}/versions/${segment(version)}/restore`, {
        expected_revision: expectedRevision,
      }),
    runJob: (data: object, key: string) =>
      submit('/ai/jobs', { ...data, project_id: projectId }, key),
    preflightJob: (data: object) =>
      send<TaskBudgetPreflight>('/ai/jobs/preflight', { ...data, project_id: projectId }),
    job: (id: string) => get<AIJob>(`/ai/jobs/${encodeURIComponent(id)}`),
    jobPreview: (id: string) => get<JobPreview>(`/ai/jobs/${encodeURIComponent(id)}/preview`),
    jobsPage: (chapterId?: string, kind: 'writing' | 'summary' = 'writing', before?: string, activeOnly = false) =>
      get<JobPage>(`/ai/jobs/page?kind=${kind}&active_only=${activeOnly}&limit=50`
        + (chapterId ? `&chapter_id=${encodeURIComponent(chapterId)}` : '')
        + (before ? `&before=${encodeURIComponent(before)}` : '')),
    cancelJob: (id: string) => send<AIJob>(`/ai/jobs/${encodeURIComponent(id)}/cancel`),
    resumeJob: (id: string, data: object, key: string) =>
      submit(`/ai/jobs/${encodeURIComponent(id)}/resume`, data, key),
    jobs: (chapterId?: string, kind: 'writing' | 'summary' = 'writing') =>
      get<AIJob[]>(
        `/ai/jobs?kind=${kind}` +
          (chapterId ? `&chapter_id=${encodeURIComponent(chapterId)}` : ""),
      ),
    acceptJob: (id: string, confirmed = false) =>
      send<ChapterVersion>(`/ai/jobs/${id}/accept`, { confirmed }),
    feedback: (data: object) =>
      send("/feedback", { ...data, project_id: projectId }),
    confirmPreference: (id: string, instruction?: string) =>
      send(`/feedback/preferences/${id}/confirm`, instruction === undefined ? {} : { instruction }),
    disablePreference: (id: string) =>
      send(`/feedback/preferences/${id}/disable`),
    decideConflict: (id: string, optionId: string, note: string) =>
      send(`/conflicts/${id}/decide`, { option_id: optionId, note }),
    resolveEntityStateConflict: (
      id: string,
      stateId: string,
      revision: number,
      note: string,
    ) => send<Conflict>(`/conflicts/${segment(id)}/resolve-entity-state`, {
      state_id: stateId,
      revision,
      note,
    }),
    generateAsset: (data: object) =>
      send<Asset>("/assets/generate", { ...data, project_id: projectId }),
    library: () => get<LibraryItem[]>("/library"),
    libraryPage: (category = 'all', cursor?: string, pinned = false, limit = 50) =>
      get<LibraryPage>(`/library/page?category=${encodeURIComponent(category)}&limit=${limit}&pinned=${pinned}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
    materialDetail: (item: MaterialReference) => get<LibraryItem>(`/library/${encodeURIComponent(item.type)}/${encodeURIComponent(item.id)}`),
    searchSummaries: (q: string, category = 'all') =>
      get<{ items: LibrarySummary[]; total: number; total_kind: 'bounded_candidates' }>(
        `/library/search?lightweight=true&include_manuscripts=true&q=${encodeURIComponent(q)}&category=${encodeURIComponent(category)}`),
    ragHealth: () => get<RagHealth>("/rag/health"),
    rebuildRag: () => send("/rag/rebuild"),
    repairProjectLedger: () => send<{ scheduled: boolean }>("/rag/repair-ledger"),
    search: (q: string, chapterId?: string) =>
      get<{ items: LibraryItem[] }>(
        `/library/search?q=${encodeURIComponent(q)}&include_manuscripts=true${chapterId ? "&chapter_id=" + chapterId : ""}`,
      ),
    createMaterial: (kind: string, data: object) =>
      send<LibraryItem>(`/library/${kind}`, data),
    editMaterial: (item: Pick<LibraryItem, 'id' | 'type'>, data: object) =>
      send<LibraryItem>(`/library/${item.type}/${item.id}`, data, "PATCH"),
    deleteMaterial: (item: MaterialReference) =>
      request<MaterialLifecycleResult>(base + `/library/${item.type}/${item.id}`, {
        method: "DELETE",
      }),
    trash: () => get<LibraryItem[]>("/trash"),
    trashPage: (cursor?: string) => get<LibraryPage>(`/trash/page?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
    restoreMaterial: (item: MaterialReference) =>
      send<MaterialLifecycleResult>(`/trash/${item.type}/${item.id}/restore`),
    purgeMaterial: (item: MaterialReference) =>
      request(base + `/trash/${item.type}/${item.id}/purge`, {
        method: "DELETE",
      }),
    completeChapter: (id: string, data: object, key: string) =>
      submit(`/chapters/${id}/complete`, data, key),
    repairLedger: (id: string) => send(`/chapters/${id}/summary/repair-ledger`),
    summary: (id: string) =>
      get<ChapterSummary | null>(`/chapters/${id}/summary`),
    editSummary: (
      id: string,
      recap: string,
      revision: number,
      details: ChapterSummary["details"] | undefined,
      summaryId: string,
    ) =>
      send<ChapterSummary>(
        `/chapters/${id}/summary`,
        { recap, revision, details, summary_id: summaryId },
        "PATCH",
      ),
    checkDrift: (id: string) => send<Conflict[]>(`/chapters/${id}/drift-check`),
    decideAlert: (id: string, decision: string, confirmed: boolean) =>
      send(`/conflicts/${id}/decision`, { decision, confirmed }),
  };
}
