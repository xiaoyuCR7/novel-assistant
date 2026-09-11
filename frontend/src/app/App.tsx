import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { FocusShell } from "../components/FocusShell";
import { ChatWorkspace } from "../features/ai/ChatWorkspace";
import { ModelSettings } from "../features/settings/ModelSettings";
import { SpendingPanel } from '../features/settings/SpendingPanel';
import { AutomaticBackups } from '../features/settings/AutomaticBackups';
import { QualityCoverage } from '../features/quality/QualityCoverage';
import type { CoverageAction } from '../lib/qualityCoverage';
import { WritingGoals } from '../features/progress/WritingGoals';
import { BackupRestore } from '../features/projects/BackupRestore';
import { ManuscriptExport } from '../features/projects/ManuscriptExport';
import { ProjectTodos } from '../features/projects/ProjectTodos';
import type { ProjectTodo } from '../lib/projectTodos';
import { LocalDraftRecovery } from '../components/LocalDraftRecovery';
import { DiagnosticError } from '../components/DiagnosticError';
import { ConversationsPanel } from '../features/ai/ConversationsPanel';
import { conversationApi } from '../lib/conversationApi';
import { ProjectPreparation } from "../features/preparation/ProjectPreparation";
import { AssetLibrary } from "../features/assets/AssetLibrary";
import { ConflictCenter } from "../features/conflicts/ConflictCenter";
import { IdeaBoard } from "../features/ideas/IdeaBoard";
import { KnowledgeBase } from "../features/knowledge/KnowledgeBase";
import { ProgressPulse } from "../features/progress/ProgressPulse";
import { ProjectLauncher } from "../features/projects/ProjectLauncher";
import { BookImportWizard, type BookImportWizardHandle } from "../features/projects/BookImportWizard";
import { ImportAnalysisCard, ImportAnalysisPanel } from "../features/projects/ImportAnalysisPanel";
import { MemoryCandidateReview } from "../features/memory/MemoryCandidateReview";
import { EntityStudio } from "../features/story/EntityStudio";
import { StoryMap } from "../features/story/StoryMap";
import { StyleLab } from "../features/style/StyleLab";
import { WikiWorkspace } from "../features/wiki/WikiWorkspace";
import { QualityWorkspace } from "../features/quality/QualityWorkspace";
import { ChapterWorkspace } from "../features/writing/ChapterWorkspace";
import { VersionPanel } from "../features/writing/VersionPanel";
import { api, ApiError, projectApi, type AIJob, type JobSummary } from "../lib/api";

import type { BoundedJsonObject, ChapterDocument, ChapterSummary, GeneratedMemoryCandidateListItem, ImportCommitResponse, ImportMemoryCandidate, LibraryItem, MaterialReference, MemoryCandidateBulkConfirmEntry, Project, StoryNode } from "../lib/types";
import { WorkspaceRail, views, type View } from "../components/WorkspaceRail";
import { readWorkspaceState, saveWorkspaceConversation, saveWorkspaceJob, saveWorkspaceState } from '../lib/workspaceState';
import { ProjectSwitcher } from "../features/projects/ProjectSwitcher";
import { MaterialSidebar } from "../features/library/MaterialSidebar";
import { refreshLibrary } from '../features/library/refreshLibrary';
import {
  MaterialEditorSheet,
  categoryNames,
} from "../features/library/MaterialEditorSheet";
import { TrashView } from "../features/library/TrashView";
import { ContextLens } from "../features/ai/ContextLens";
import { ChapterSummaryPanel } from "../features/writing/ChapterSummaryPanel";
import { DriftAlertCenter } from "../features/conflicts/DriftAlertCenter";
import { Modal } from "../components/Modal";
import { Icon } from "../components/Icon";
import { RagStatus } from "../features/ai/RagStatus";
import { useProjectJobs } from '../features/ai/useProjectJobs';
import { JobStatus } from '../features/ai/JobStatus';
import { acknowledge, pendingSubmissions, prepareSubmission, type PendingSubmission } from '../features/ai/pendingSubmission';
import { clearInputBudget, inputBudgetValue, modelInputBudget, readInputBudget, requireFittingPreflight, resolveInputBudget, saveInputBudget } from '../features/ai/inputBudget';
import { blocksSummaryGeneration, hasPublishedSummary } from '../features/ai/jobSemantics';

type MemoryCandidateCommand =
  | { action: "edit"; id: string; revision: number; payload: BoundedJsonObject; evidence: GeneratedMemoryCandidateListItem["evidence"];
    scope: { projectId: string; chapterId: string } }
  | { action: "confirm" | "reject"; id: string; revision: number;
    scope: { projectId: string; chapterId: string } };

type ImportCandidateCommand =
  | { action: "edit"; id: string; revision: number; payload: BoundedJsonObject; evidence: ImportMemoryCandidate["evidence"] }
  | { action: "confirm"; id: string; revision: number; resolution?: "create_separate" }
  | { action: "reject"; id: string; revision: number }
  | { action: "bulk"; id: "bulk"; entries: MemoryCandidateBulkConfirmEntry[] };

export function App() {
  const queryClient = useQueryClient();
  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
  });
  const [active, setActive] = useState(() => {
    try {
      return localStorage.getItem("studio:active-project");
    } catch {
      return null;
    }
  });
  const [creating, setCreating] = useState(false),
    [importing, setImporting] = useState(false),
    [dirty, setDirty] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const importWizardRef = useRef<BookImportWizardHandle>(null);
  const project =
    projectsQuery.data?.find((p) => p.id === active) ?? projectsQuery.data?.[0];
  function activateProject(next: Project) {
    queryClient.setQueryData<Project[]>(["projects"], (current = []) => [
      next,
      ...current.filter((project) => project.id !== next.id),
    ]);
    setActive(next.id);
    try {
      localStorage.setItem("studio:active-project", next.id);
    } catch {
      /* Session-only selection. */
    }
  }
  const createProject = useMutation({
    mutationFn: api.createProject,
    onSuccess: async (p) => {
      activateProject(p);
      setCreating(false);
      setDirty(false);
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
  async function activateImportedProject(result: ImportCommitResponse) {
    activateProject(result.project);
    setDirty(false);
    setCreating(false);
    setImporting(false);
    await queryClient.invalidateQueries({ queryKey: ["projects"] });
  }
  function switchProject(id: string) {
    if (dirty && !window.confirm("当前章节有未保存修改。确定切换小说？"))
      return;
    setDirty(false);
    setActive(id);
    try {
      localStorage.setItem("studio:active-project", id);
    } catch {
      /* Session-only selection. */
    }
  }
  function closeImport() {
    if (importWizardRef.current) void importWizardRef.current.cancel();
    else setImporting(false);
  }
  const restoreDialog = restoring && <Modal title="从备份恢复小说" onClose={() => { if (!restoreBusy) setRestoring(false); }}>
    <BackupRestore onBusyChange={setRestoreBusy} onCancel={() => setRestoring(false)} onRestored={async restoredProject => {
      activateProject(restoredProject); setRestoring(false); setDirty(false);
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
    }} />
  </Modal>;
  if (projectsQuery.isLoading)
    return <div className="app-loading">正在打开本地创作室…</div>;
  if (projectsQuery.error && !projectsQuery.data)
    return (
      <main className="error-page">
        <h1>无法连接本地服务</h1>
        <DiagnosticError error={projectsQuery.error} />
        <button onClick={() => projectsQuery.refetch()}>重新连接</button>
      </main>
    );
  if (!project)
    return (
      <>
        <ProjectLauncher
          busy={createProject.isPending}
          onImport={() => setImporting(true)}
          onCreate={async (draft) => {
            await createProject.mutateAsync(draft);
          }}
        />
        <div className="project-tool-links"><button onClick={() => setRestoring(true)}>从备份恢复小说</button></div>
        {restoreDialog}
        {importing && (
          <Modal title="导入已有小说" onClose={closeImport}>
            <BookImportWizard ref={importWizardRef} onComplete={activateImportedProject} onCancel={() => setImporting(false)} />
          </Modal>
        )}
      </>
    );
  return (
    <>
      {projectsQuery.error && (
        <div>小说列表刷新失败，当前编辑已保留：<DiagnosticError error={projectsQuery.error} />
          <button onClick={() => projectsQuery.refetch()}>重试列表刷新</button>
        </div>
      )}
      <ProjectStudio
        key={project.id}
        projectId={project.id}
        projects={projectsQuery.data ?? []}
        onProjectChange={switchProject}
        onCreate={() => setCreating(true)}
        onRestore={() => {
          if (dirty && !window.confirm('当前章节有未保存修改，恢复后将切换项目。确定打开备份恢复？')) return;
          setRestoring(true);
        }}
        onDirtyChange={setDirty}
        dirty={dirty}
      />
      {restoreDialog}
      {creating && (
        <Modal title="创建独立小说项目" onClose={() => setCreating(false)}>
          <ProjectLauncher
            busy={createProject.isPending}
            onImport={() => {
              setCreating(false);
              setImporting(true);
            }}
            onCreate={async (draft) => {
              if (
                dirty &&
                !window.confirm("有未保存正文，仍要创建并切换项目？")
              )
                return;
              await createProject.mutateAsync(draft);
            }}
          />
        </Modal>
      )}
      {importing && (
        <Modal title="导入已有小说" onClose={closeImport}>
          <BookImportWizard
            ref={importWizardRef}
            beforeCommit={() => !dirty || window.confirm("当前章节有未保存修改。确定创建导入项目并切换小说？")}
            onComplete={activateImportedProject}
            onCancel={() => setImporting(false)}
          />
        </Modal>
      )}
    </>
  );
}

function ProjectStudio({
  projectId,
  projects,
  onProjectChange,
  onCreate,
  onRestore,
  onDirtyChange,
  dirty,
}: {
  projectId: string;
  projects: Project[];
  onProjectChange: (id: string) => void;
  onCreate: () => void;
  onRestore: () => void;
  onDirtyChange: (dirty: boolean) => void;
  dirty: boolean;
}) {
  const client = useMemo(() => projectApi(projectId), [projectId]);
  const queryClient = useQueryClient();
  const [restored] = useState(() => readWorkspaceState(projectId));
  const [view, setView] = useState<View>(() => views.find(item => item[0] === restored.view)?.[0] ?? 'ai'),
    [selectedNodeId, setSelectedNodeId] = useState<string | null>(restored.selectedNodeId ?? null);
  useEffect(() => { saveWorkspaceState(projectId, { view }); }, [projectId, view]);
  // Pin only the active dirty session's identity; the editors still own their drafts.
  const [draftChapter, setDraftChapter] = useState<StoryNode>();
  const [job, setJob] = useState<AIJob | null>(null),
    [notice, setNotice] = useState<ReactNode>(""),
    [working, setWorking] = useState(false);
  const [candidatePending, setCandidatePending] = useState(false);
  const [preparationDecision, setPreparationDecision] = useState<{
    unresolved: number;
    resolve: (allowed: boolean) => void;
  } | null>(null);
  const preparationBypass = useRef("");
  const [importReviewOpen, setImportReviewOpen] = useState(false);
  const [query, setQuery] = useState(""),
    [debounced, setDebounced] = useState("");
  const [referenceRequest, setReferenceRequest] = useState(0);
  const selectionGeneration = useRef(0);
  const [detailTarget, setDetailTarget] = useState<MaterialReference | null>(null);
  const [detailPending, setDetailPending] = useState(false), [detailError, setDetailError] = useState('');
  const [category, setCategory] = useState('all');
  const [replacement, setReplacement] = useState<{ chapterId: string; document: ChapterDocument; draft: string }>();
  const workingLock = useRef(false);
  const candidatePendingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const [manuscriptOpen, setManuscriptOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false), [draftRecoveryOpen, setDraftRecoveryOpen] = useState(false);
  const [conversationPanelOpened, setConversationPanelOpened] = useState(false);
  const [todosOpen, setTodosOpen] = useState(false), [qualityTargetRunId, setQualityTargetRunId] = useState<string>();
  const [coverageOpen, setCoverageOpen] = useState(false), [goalsOpen, setGoalsOpen] = useState(false);
  const [freshQualityReview, setFreshQualityReview] = useState(0);
  const todoRequest = useRef(0);
  const [pending, setPending] = useState(() => pendingSubmissions(projectId));
  const [inputBudget, setInputBudget] = useState(() => {
    const saved = readInputBudget(projectId);
    return { value: saved ?? '', overridden: saved !== undefined };
  });
  const published = useRef(new Map<string, string>());
  const [selected, setSelected] = useState<LibraryItem | null>(null),
    [editing, setEditing] = useState<LibraryItem | null | undefined>(undefined),
    [trashOpen, setTrashOpen] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 180);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        document.getElementById("material-search")?.focus();
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    function leave(e: BeforeUnloadEvent) {
      if (dirty || working || candidatePending) {
        e.preventDefault();
        e.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty, working, candidatePending]);
  const workspaceQuery = useQuery({
    queryKey: ["workspace", projectId, 'navigation'],
    queryFn: client.navigation,
  });
  const preparationQuery = useQuery({
    queryKey: ["preparation", projectId],
    queryFn: client.preparation,
    enabled: ["ai", "write", "preparation"].includes(view),
  });
  const ideasView = useQuery({ queryKey: ['workspace', projectId, 'ideas'], queryFn: () => client.workspaceView('ideas'), enabled: view === 'ideas' });
  const storyView = useQuery({ queryKey: ['workspace', projectId, 'story'], queryFn: () => client.workspaceView('story'), enabled: view === 'story' });
  const entitiesView = useQuery({ queryKey: ['workspace', projectId, 'entities'], queryFn: () => client.workspaceView('entities'), enabled: view === 'entities' });
  const knowledgeView = useQuery({ queryKey: ['workspace', projectId, 'knowledge'], queryFn: () => client.workspaceView('knowledge'), enabled: view === 'knowledge' });
  const styleView = useQuery({ queryKey: ['workspace', projectId, 'style'], queryFn: () => client.workspaceView('style'), enabled: view === 'style' });
  const libraryView = useQuery({ queryKey: ['workspace', projectId, 'library'], queryFn: () => client.workspaceView('library'), enabled: view === 'library' || editing !== undefined });
  const workspace = workspaceQuery.data;
  const project = workspace?.project;
  const chapterNodes =
    workspace?.nodes.filter((n) => n.kind === "chapter") ?? [];
  const selectedNode =
    workspace?.nodes.find((n) => n.id === selectedNodeId) ?? chapterNodes[0];
  const chapterNode = draftChapter
    ? chapterNodes.find(node => node.id === draftChapter.id) ?? draftChapter
    : selectedNode?.kind === "chapter" ? selectedNode : chapterNodes[0];
  const chapterId =
    selectedNodeId === "project-chat"
      ? undefined
      : chapterNode?.id;
  const sourceUnavailable = !!chapterId && !chapterNodes.some(node => node.id === chapterId);
  useEffect(() => {
    if (!workspace || draftChapter || dirty) return;
    const next = selectedNodeId === 'project-chat' || workspace.nodes.some(node => node.id === selectedNodeId)
      ? selectedNodeId! : workspace.nodes.find(node => node.kind === 'chapter')?.id ?? 'project-chat';
    if (next !== selectedNodeId) setSelectedNodeId(next);
    saveWorkspaceState(projectId, { selectedNodeId: next });
  }, [workspace, selectedNodeId, projectId, draftChapter, dirty]);
  function hasChapterSource(id = chapterId) {
    const navigation = queryClient.getQueryData<{ nodes: StoryNode[] }>(["workspace", projectId, 'navigation']);
    return !id || !!navigation?.nodes.some(node => node.kind === 'chapter' && node.id === id);
  }
  function requireChapterSource(id = chapterId) {
    if (!hasChapterSource(id)) throw Error('当前章节不可用，请先恢复章节或复制草稿后切换；未发送请求。');
  }
  const editSession = useMemo(() => ({
    active: true, draft: "", manuscriptDirty: false, summaryDirty: false,
  }), [projectId, chapterId]);
  useEffect(() => {
    editSession.active = true;
    return () => { editSession.active = false; };
  }, [editSession]);
  const manuscriptDirtyChange = useCallback((next: boolean) => {
    if (!editSession.active) return;
    editSession.manuscriptDirty = next;
    const retain = next || editSession.summaryDirty || sourceUnavailable;
    setDraftChapter(retain ? chapterNode : undefined);
    onDirtyChange(retain);
  }, [editSession, onDirtyChange, chapterNode, sourceUnavailable]);
  const summaryDirtyChange = useCallback((next: boolean) => {
    if (!editSession.active) return;
    editSession.summaryDirty = next;
    const retain = next || editSession.manuscriptDirty || sourceUnavailable;
    setDraftChapter(retain ? chapterNode : undefined);
    onDirtyChange(retain);
  }, [editSession, onDirtyChange, chapterNode, sourceUnavailable]);
  const draftChange = useCallback((draft: string) => { editSession.draft = draft; }, [editSession]);
  function clearDirty() {
    editSession.manuscriptDirty = false;
    editSession.summaryDirty = false;
    setDraftChapter(undefined);
    onDirtyChange(false);
  }
  function beginWorking() {
    if (requestPending()) throw Error("请等待当前请求结束后再操作。");
    workingLock.current = true;
    setWorking(true);
  }
  function endWorking() {
    workingLock.current = false;
    if (mounted.current) setWorking(false);
  }
  function requestPending() {
    return workingLock.current || candidatePendingRef.current;
  }
  const chapterQuery = useQuery({
    queryKey: ["chapter", projectId, chapterId],
    queryFn: () => client.chapter(chapterId!),
    enabled: !!chapterId && !sourceUnavailable,
  });
  const conversationScope = chapterId ?? 'project';
  const [conversationSelections, setConversationSelections] = useState<Record<string, string>>(restored.conversationSelections ?? {});
  const selectedConversationId = conversationSelections[conversationScope];
  const conversations = useMemo(() => conversationApi(projectId), [projectId]);
  const conversationQuery = useQuery({ queryKey: ['conversation', projectId, selectedConversationId],
    queryFn: ({ signal }) => conversations.detail(selectedConversationId!, signal), enabled: !!selectedConversationId });
  const conversationId = conversationQuery.data?.is_default ? undefined : selectedConversationId;
  const conversationUnavailable = !!selectedConversationId && (!conversationQuery.data || !!conversationQuery.error ||
    conversationQuery.data.status !== 'active' || (conversationQuery.data.chapter_id ?? null) !== (chapterId ?? null));
  async function selectConversation(id: string) {
    const next = await conversations.detail(id);
    if (next.status !== 'active' || (next.chapter_id ?? null) !== (chapterId ?? null)) throw Error('会话不属于当前写作范围或已归档。');
    queryClient.setQueryData(['conversation', projectId, id], next);
    setConversationSelections(current => ({ ...current, [conversationScope]: id }));
    saveWorkspaceConversation(projectId, conversationScope, id);
    setJob(null);
  }
  const jobsQuery = useProjectJobs(projectId, chapterId, 'writing', conversationId, !conversationUnavailable);
  const [jobSelections, setJobSelections] = useState<Record<string, string>>(restored.jobSelections ?? {});
  const [missingJobs, setMissingJobs] = useState<Set<string>>(() => new Set());
  const jobScope = `${projectId}:${chapterId ?? 'project'}${conversationId ? `:${conversationId}` : ''}`;
  function setJobSelection(next: { scope: string; id: string } | null) {
    const scope = next?.scope ?? jobScope;
    setJobSelections(current => {
      const updated = { ...current }; delete updated[scope];
      if (next) updated[scope] = next.id;
      return updated;
    });
    saveWorkspaceJob(projectId, scope, next?.id);
  }
  const historyJobs = (jobsQuery.data ?? []).filter(item => !missingJobs.has(`${jobScope}:${item.id}`));
  const selectedJobId = jobSelections[jobScope] ?? historyJobs.at(-1)?.id;
  const knownJobSummary = historyJobs.find(item => item.id === selectedJobId);
  const selectedJobQuery = useQuery({
    queryKey: ['job-detail', projectId, selectedJobId, knownJobSummary?.status,
      knownJobSummary?.control_revision, knownJobSummary?.accepted_version_id, ...(knownJobSummary ? [] : [jobScope]), ...(conversationId ? [conversationId] : [])],
    queryFn: async () => {
      const value = await client.job(selectedJobId!, conversationId);
      if (!knownJobSummary && (value.id !== selectedJobId || value.project_id !== projectId ||
          (value.chapter_id ?? null) !== (chapterId ?? null) ||
          !['chat', 'continue', 'full_chapter', 'plan', 'draft', 'review', 'suggest', 'rewrite', 'scene_description'].includes(value.task_type)))
        throw new ApiError(404, '原对话已不在当前章节中。');
      return value;
    },
    enabled: !!workspace && !conversationUnavailable && !!selectedJobId && (!knownJobSummary || knownJobSummary.status === 'succeeded' || !!jobSelections[jobScope]),
    staleTime: Infinity,
  });
  const missingJob = selectedJobQuery.error instanceof ApiError && selectedJobQuery.error.status === 404;
  useEffect(() => {
    if (!selectedJobId || !workspace) return;
    if (missingJob) {
      setMissingJobs(current => new Set([...current, `${jobScope}:${selectedJobId}`]));
      setJobSelections(current => {
        if (current[jobScope] !== selectedJobId) return current;
        const next = { ...current }; delete next[jobScope]; return next;
      });
      saveWorkspaceJob(projectId, jobScope);
    } else if (selectedJobQuery.data) saveWorkspaceJob(projectId, jobScope, selectedJobId);
  }, [selectedJobId, selectedJobQuery.data, missingJob, jobScope, projectId, workspace]);
  const selectedJobSummary = knownJobSummary ?? (missingJob ? undefined : selectedJobQuery.data);
  const visibleJobs = !knownJobSummary && selectedJobSummary
    ? [...historyJobs, selectedJobSummary].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '') || a.id.localeCompare(b.id))
    : historyJobs;
  const summaryJobs = useProjectJobs(projectId, chapterId, 'summary');
  const modelQuery = useQuery({
    queryKey: ["model-settings"],
    queryFn: api.modelSettings,
  });
  let automaticInputBudget = '', modelBudgetError = '';
  try { automaticInputBudget = String(modelInputBudget(modelQuery.data)); }
  catch (cause) { modelBudgetError = cause instanceof Error ? cause.message : String(cause); }
  const displayedInputBudget = inputBudget.overridden ? inputBudget.value : automaticInputBudget;
  function newTaskInputBudget() {
    if (modelQuery.error) throw Error('模型配置不可用，请打开设置检查。');
    return resolveInputBudget(modelQuery.data, inputBudget.overridden ? inputBudget.value : undefined);
  }
  const versionsQuery = useInfiniteQuery({
    queryKey: ["versions", projectId, chapterId],
    queryFn: ({ pageParam }) => client.versionPage(chapterId!, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!chapterId,
  });
  const versions = useMemo(
    () => versionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [versionsQuery.data],
  );
  const versionLoadError = versionsQuery.isFetchNextPageError
    ? "加载更多版本失败，请重试。"
    : versionsQuery.isLoadingError
      ? "版本记录加载失败，请重试。"
      : versionsQuery.isRefetchError
        ? "版本记录刷新失败，请重试。"
        : versionsQuery.isError
          ? "版本记录加载失败，请重试。"
          : undefined;
  const summaryQuery = useQuery({
    queryKey: ["summary", projectId, chapterId],
    queryFn: () => client.summary(chapterId!),
    enabled: !!chapterId,
  });
  const [retainedSummary, setRetainedSummary] = useState<{ chapterId: string; summary: ChapterSummary } | null>(null);
  useEffect(() => {
    if (chapterId && summaryQuery.data) setRetainedSummary({ chapterId, summary: summaryQuery.data });
  }, [chapterId, summaryQuery.data]);
  const visibleSummary = summaryQuery.data ?? (retainedSummary && retainedSummary.chapterId === chapterId ? retainedSummary.summary : null);
  const memoryCandidatesQuery = useInfiniteQuery({
    queryKey: ["memory-candidates", projectId, chapterId],
    queryFn: ({ pageParam }) => client.memoryCandidates(pageParam, chapterId!, "pending", 100),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!chapterId && !!visibleSummary,
  });
  const memoryCandidates = useMemo(
    () => memoryCandidatesQuery.data?.pages.flatMap((page) => page.items ?? [])
      .filter((candidate): candidate is GeneratedMemoryCandidateListItem => candidate.origin === "generated" && candidate.status === "pending") ?? [],
    [memoryCandidatesQuery.data],
  );
  const importAnalysisQuery = useQuery({
    queryKey: ["import-analysis", projectId],
    queryFn: async () => {
      try {
        const analysis = await client.latestImportAnalysis();
        return analysis && !Array.isArray(analysis) && analysis.progress ? analysis : null;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    refetchInterval: (query) => query.state.data?.status === "analyzing" ? 2_000 : false,
  });
  const pendingImportCandidateCountQuery = useQuery({
    queryKey: ["import-memory-candidate-count", projectId],
    queryFn: async () => {
      const page = await client.memoryCandidates(undefined, undefined, "pending", 1, "import");
      return (page.counts.pending ?? 0) + (page.counts.conflict ?? 0);
    },
    enabled: !!importAnalysisQuery.data,
  });
  const pendingImportCandidatesQuery = useInfiniteQuery({
    queryKey: ["import-memory-candidates", projectId, "pending"],
    queryFn: ({ pageParam }) => client.memoryCandidates(pageParam, undefined, "pending", 100, "import"),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: importReviewOpen,
  });
  const conflictImportCandidatesQuery = useInfiniteQuery({
    queryKey: ["import-memory-candidates", projectId, "conflict"],
    queryFn: ({ pageParam }) => client.memoryCandidates(pageParam, undefined, "conflict", 100, "import"),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: importReviewOpen,
  });
  const previousImportProgress = useRef<{ projectId: string; signature: string } | null>(null);
  useEffect(() => {
    const analysis = importAnalysisQuery.data;
    if (!analysis) {
      previousImportProgress.current = null;
      return;
    }
    const signature = [analysis.id, analysis.progress.completed, analysis.progress.failed].join(":");
    const previous = previousImportProgress.current;
    previousImportProgress.current = { projectId, signature };
    if (!previous || previous.projectId !== projectId || previous.signature === signature) return;
    void queryClient.invalidateQueries({
      queryKey: ["import-memory-candidate-count", projectId],
      refetchType: "active",
    });
  }, [
    importAnalysisQuery.data?.id,
    importAnalysisQuery.data?.status,
    importAnalysisQuery.data?.progress.completed,
    importAnalysisQuery.data?.progress.failed,
    projectId,
    queryClient,
  ]);
  const importCandidates = useMemo(
    () => Array.from(new Map([
      ...(pendingImportCandidatesQuery.data?.pages.flatMap((page) => page.items ?? []) ?? []),
      ...(conflictImportCandidatesQuery.data?.pages.flatMap((page) => page.items ?? []) ?? []),
    ].filter((candidate): candidate is ImportMemoryCandidate => candidate.origin === "import")
      .map((candidate) => [candidate.id, candidate])).values()),
    [pendingImportCandidatesQuery.data, conflictImportCandidatesQuery.data],
  );
  const importCandidateQueryError = pendingImportCandidatesQuery.error ?? conflictImportCandidatesQuery.error;
  const hasMoreImportCandidates = !!pendingImportCandidatesQuery.hasNextPage || !!conflictImportCandidatesQuery.hasNextPage;
  const pendingImportCandidateCount = pendingImportCandidateCountQuery.data ?? 0;
  const importAnalysisMutation = useMutation({
    mutationFn: ({ action, revision }: { action: "pause" | "continue" | "retry"; revision: number }) => {
      const analysis = importAnalysisQuery.data;
      if (!analysis) throw new Error("导入分析状态尚未加载。");
      if (action === "pause") return client.pauseImportAnalysis(analysis.id, revision);
      if (action === "continue") return client.continueImportAnalysis(analysis.id, revision);
      return client.retryImportAnalysis(analysis.id, revision);
    },
    onSuccess: (analysis) => queryClient.setQueryData(["import-analysis", projectId], analysis),
    onError: async (error) => {
      if (error instanceof ApiError && (
        error.code === "IMPORT_ANALYSIS_REVISION_CONFLICT" || error.code === "revision_conflict"
      )) {
        await queryClient.invalidateQueries({ queryKey: ["import-analysis", projectId] });
      }
    },
  });
  const importCandidateMutation = useMutation<unknown, Error, ImportCandidateCommand>({
    mutationFn: (command: ImportCandidateCommand) => {
      if (command.action === "bulk") return client.bulkConfirmMemoryCandidates(command.entries);
      if (command.action === "edit") return client.editMemoryCandidate(command.id, {
        revision: command.revision,
        payload: command.payload,
        evidence: command.evidence,
      });
      if (command.action === "confirm") return client.confirmMemoryCandidate(command.id, command.revision, command.resolution);
      return client.rejectMemoryCandidate(command.id, command.revision);
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["import-memory-candidates", projectId], refetchType: "active" }),
        queryClient.invalidateQueries({ queryKey: ["import-memory-candidate-count", projectId], refetchType: "active" }),
        queryClient.invalidateQueries({ queryKey: ["workspace", projectId] }),
        refreshLibrary(queryClient, projectId),
        queryClient.invalidateQueries({ queryKey: ["context", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["summary", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["conflicts", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["progress", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["import-analysis", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["style-rules", projectId] }),
        queryClient.invalidateQueries({ queryKey: ["preferences", projectId] }),
      ]);
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.code === "revision_conflict") {
        await queryClient.invalidateQueries({
          queryKey: ["import-memory-candidates", projectId],
          refetchType: "active",
        });
      }
    },
  });
  const memoryCandidateLoadError = memoryCandidatesQuery.isFetchNextPageError
    ? "加载更多待确认记忆失败，请重试。"
    : memoryCandidatesQuery.isLoadingError
      ? "待确认记忆加载失败，请重试。"
      : memoryCandidatesQuery.isRefetchError
        ? "待确认记忆刷新失败，已加载内容仍保留。"
        : memoryCandidatesQuery.isError
          ? "待确认记忆加载失败，请重试。"
          : "";
  const memoryCandidateMutation = useMutation({
    mutationFn: (command: MemoryCandidateCommand) => {
      const scopedClient = projectApi(command.scope.projectId);
      if (command.action === "edit") {
        return scopedClient.editMemoryCandidate(command.id, {
          revision: command.revision,
          payload: command.payload,
          evidence: command.evidence,
        });
      }
      return command.action === "confirm"
        ? scopedClient.confirmMemoryCandidate(command.id, command.revision)
        : scopedClient.rejectMemoryCandidate(command.id, command.revision);
    },
    onSuccess: async (_candidate, command) => {
      const { projectId: sourceProjectId, chapterId: sourceChapterId } = command.scope;
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["memory-candidates", sourceProjectId, sourceChapterId],
          refetchType: "active",
        }),
        queryClient.invalidateQueries({ queryKey: ["workspace", sourceProjectId] }),
        refreshLibrary(queryClient, sourceProjectId),
        queryClient.invalidateQueries({ queryKey: ["context", sourceProjectId] }),
        queryClient.invalidateQueries({ queryKey: ["conflicts", sourceProjectId] }),
      ]);
    },
    onError: async (error, command) => {
      if (!(error instanceof ApiError) || error.code !== "revision_conflict") return;
      await queryClient.invalidateQueries({
        queryKey: ["memory-candidates", command.scope.projectId, command.scope.chapterId],
        refetchType: "active",
      });
    },
  });
  async function runCandidateMutation<T>(action: () => Promise<T>) {
    if (candidatePendingRef.current) throw new Error("已有候选记忆操作正在进行。");
    candidatePendingRef.current = true;
    setCandidatePending(true);
    try {
      return await action();
    } finally {
      candidatePendingRef.current = false;
      if (mounted.current) setCandidatePending(false);
    }
  }
  async function runMemoryCandidateMutation(command: MemoryCandidateCommand) {
    return runCandidateMutation(() => memoryCandidateMutation.mutateAsync(command));
  }
  async function runImportCandidateMutation(command: ImportCandidateCommand) {
    return runCandidateMutation(() => importCandidateMutation.mutateAsync(command));
  }
  const libraryQuery = useInfiniteQuery({
    queryKey: ['library', projectId, 'page', category],
    queryFn: ({ pageParam }) => client.libraryPage(category, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor ?? undefined,
  });
  const pinnedQuery = useQuery({ queryKey: ['library', projectId, 'pinned'], queryFn: () => client.libraryPage('all', undefined, true, 5) });
  const ledgerQuery = useInfiniteQuery({
    queryKey: ['library', projectId, 'ledger'],
    queryFn: ({ pageParam }) => client.libraryPage('summary', pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor ?? undefined,
  });
  const searchQuery = useQuery({
    queryKey: ['library', projectId, 'search', debounced, category],
    queryFn: () => client.searchSummaries(debounced, category),
    enabled: !!debounced,
  });
  const materialDetailQuery = useQuery({
    queryKey: ['library', projectId, 'detail', detailTarget?.type, detailTarget?.id],
    queryFn: () => client.materialDetail(detailTarget!),
    enabled: !!detailTarget && editing === undefined,
    staleTime: Infinity,
  });
  const detailGeneration = selectionGeneration.current;
  useEffect(() => {
    // The scoped observer follows invalidations only while previewing. Editors own
    // their draft until closed; switching/closing detaches the old source observer.
    if (!detailTarget || editing !== undefined || detailGeneration !== selectionGeneration.current) return;
    if (materialDetailQuery.error) {
      setSelected(null);
      setDetailError(materialDetailQuery.error.message);
      setDetailPending(materialDetailQuery.isFetching);
    } else if (materialDetailQuery.data) {
      setSelected(materialDetailQuery.data);
      setDetailError('');
      setDetailPending(false);
    }
  }, [detailTarget, detailGeneration, editing, materialDetailQuery.data, materialDetailQuery.error, materialDetailQuery.isFetching]);
  const trashQuery = useInfiniteQuery({
    queryKey: ['library', projectId, 'trash'],
    queryFn: ({ pageParam }) => client.trashPage(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor ?? undefined,
    enabled: trashOpen,
  });
  const progressQuery = useQuery({
    queryKey: ["progress", projectId],
    queryFn: client.progress,
  });
  const assetsQuery = useQuery({
    queryKey: ["assets", projectId],
    queryFn: client.assets,
    enabled: view === 'assets',
  });
  const preferencesQuery = useQuery({
    queryKey: ["preferences", projectId],
    queryFn: client.preferences,
    enabled: view === "style",
  });
  const styleRulesQuery = useQuery({
    queryKey: ["style-rules", projectId],
    queryFn: client.activeStyleRules,
    enabled: view === "style",
  });
  const conflictsQuery = useQuery({
    queryKey: ["conflicts", projectId],
    queryFn: client.conflicts,
  });
  const openConflicts = (conflictsQuery.data ?? []).filter(
    (conflict) => conflict.status === "open",
  );
  const libraryItems = libraryQuery.data?.pages.flatMap(page => page.items) ?? [];
  const libraryCounts = libraryQuery.data?.pages[0].counts ?? {};
  const libraryTotal = libraryQuery.data?.pages[0].total ?? 0;
  useEffect(() => {
    for (const current of [...(jobsQuery.data ?? []), ...(summaryJobs.data ?? [])]) {
      const stamp = JSON.stringify([current.status, current.effects]);
      if (published.current.get(current.id) === stamp) continue;
      published.current.set(current.id, stamp);
      const contentReview = current.task_type === 'chapter_summary'
        && current.status === 'recovery_required'
        && current.recovery_reason === 'content_review_required';
      if (current.status !== 'succeeded' && !hasPublishedSummary(current) && !contentReview) continue;
      const scoped = current.chapter_id ?? undefined;
      const keys = current.task_type === 'chapter_summary'
        ? [['chapter', projectId, scoped], ['summary', projectId, scoped], ['versions', projectId, scoped],
          ['library', projectId], ['progress', projectId], ['workspace', projectId], ['conflicts', projectId]]
        : [['conflicts', projectId]];
      for (const queryKey of keys) {
        if (queryKey[0] === 'library') void refreshLibrary(queryClient, projectId);
        else void queryClient.invalidateQueries({ queryKey });
      }
    }
  }, [jobsQuery.data, summaryJobs.data, projectId, queryClient, client]);

  async function deliver(item: PendingSubmission) {
    requireChapterSource(item.chapterId ?? undefined);
    if (!item.persisted && !window.confirm('浏览器无法保存待提交记录。刷新后无法安全重试，仍要提交吗？'))
      throw Error('未提交；请先恢复浏览器存储。');
    let receipt: AIJob;
    try {
      if (item.operation === 'writing') receipt = await client.runJob(item.command, item.idempotencyKey);
      else if (item.operation === 'summary' && item.chapterId)
        receipt = await client.completeChapter(item.chapterId, item.command, item.idempotencyKey);
      else if (item.operation.startsWith('resume:'))
        receipt = await client.resumeJob(item.operation.slice(7), item.command, item.idempotencyKey);
      else throw Error('无法识别待提交操作，未发送请求。');
      if (!receipt.id) throw Error('未收到有效回执，请使用原请求重试。');
      acknowledge(item);
      const kind = receipt.task_type === 'chapter_summary' ? 'summary' : 'writing';
      await queryClient.invalidateQueries({ queryKey: ['jobs', projectId, item.chapterId ?? undefined, kind] });
      return receipt;
    } catch (error) {
      // A definite validation/revision rejection is not a lost acknowledgment.
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) acknowledge(item);
      if (item.operation === 'summary' && error instanceof ApiError && error.code === 'SUMMARY_TASK_ACTIVE') {
        await queryClient.invalidateQueries({
          queryKey: ['jobs', projectId, item.chapterId ?? undefined, 'summary'],
        });
        setNotice(error.jobId
          ? `本章已有未解决的总结任务 ${error.jobId}，请在任务卡片中恢复、取消或替换。`
          : '本章已有未解决的总结任务，请刷新任务列表后处理。');
      }
      throw error;
    } finally { setPending(pendingSubmissions(projectId)); }
  }

  async function resumeTask(task: JobSummary, confirm: boolean) {
    if (task.inherited) throw Error('继承记录为只读。');
    requireChapterSource(task.chapter_id ?? undefined);
    beginWorking();
    try {
      const item = prepareSubmission(projectId, task.chapter_id ?? null, {
        expected_control_revision: task.control_revision, confirm_unknown: confirm,
      }, `resume:${task.id}`);
      await deliver(item);
    } finally { endWorking(); }
  }
  async function cancelTask(task: JobSummary) {
    if (task.inherited) throw Error('继承记录为只读。');
    await client.cancelJob(task.id);
    await queryClient.invalidateQueries({ queryKey: ['jobs', projectId, task.chapter_id ?? undefined] });
  }
  async function preflightWriting(command: Record<string, unknown>) {
    const budget = inputBudgetValue(String(command.token_budget));
    const report = await client.preflightJob(command);
    if (!mounted.current || !editSession.active)
      throw Error('创作范围已切换，未提交原任务。');
    requireChapterSource();
    if (editSession.manuscriptDirty || editSession.summaryDirty)
      throw Error('预检期间的新输入已保留，请先保存正文与总结后再提交。');
    requireFittingPreflight(report, budget);
  }
  async function replaceTask(task: JobSummary) {
    if (task.inherited) throw Error('继承记录为只读，请在当前会话新建任务。');
    requireChapterSource();
    if (editSession.manuscriptDirty || editSession.summaryDirty)
      throw Error('请先保存正文与总结，再按当前设置新建任务。');
    beginWorking();
    try {
      // History summaries intentionally truncate instructions; clone only a fresh full detail.
      const source = await client.job(task.id);
      if (!editSession.active) return;
      requireChapterSource();
      if (source.project_id !== projectId || (source.chapter_id ?? null) !== (chapterId ?? null)
        || source.task_type !== task.task_type) throw Error('原任务不属于当前创作范围。');
      if (!source.allowed_actions?.includes('replace') || hasPublishedSummary(source))
        throw Error('原任务状态已改变，不能新建替代任务；已保存总结请仅修复本地账本。');
      const summary = source.task_type === 'chapter_summary';
      if (summary && source.status !== 'cancelled') throw Error('请先取消原总结任务。');
      const current = chapterId ? await client.chapter(chapterId) : undefined;
      if (!editSession.active) return;
      requireChapterSource();
      if (editSession.manuscriptDirty || editSession.summaryDirty)
        throw Error('新输入已保留，请先保存正文与总结，再按当前设置新建任务。');
      const unknown = source.replacement_requires_confirmation ?? source.recovery_reason === 'result_unknown';
      const warning = unknown ? '原任务结果未知，可能已经完成或计费，新建可能产生重复费用。\n' : '';
      const budget = summary ? undefined : newTaskInputBudget();
      const budgetNote = summary ? '' : `本次输入预算 ${budget} Token${inputBudget.overridden ? '（本项目省费上限）' : '（跟随模型容量）'}。`;
      if (!window.confirm(`${warning}按当前设置与已保存正文新建任务？${budgetNote}将重新检索资料，并可能再次调用模型及计费。原任务和失败记录保留，不会自动取消。`)) return;
      const link = { replaces_job_id: source.id, confirm_unknown: unknown };
      const command = summary
        ? { expected_revision: current!.revision, ...link }
        : { chapter_id: chapterId ?? null, task_type: source.task_type,
          instructions: source.instructions ?? '', token_budget: budget,
          expected_revision: current?.revision ?? null, ...(conversationId ? { conversation_id: conversationId } : {}), ...link };
      if (!summary) await preflightWriting(command);
      const item = prepareSubmission(projectId, chapterId ?? null, command, summary ? 'summary' : 'writing');
      const next = await deliver(item);
      if (editSession.active) {
        if (!summary) { setJob(next); setJobSelection({ scope: jobScope, id: next.id }); }
        setNotice(`已按当前设置新建任务 ${next.id}，关联原任务 ${source.id}。`);
      }
    } finally { endWorking(); }
  }
  async function refreshWriting(keys = [
    "workspace", "chapter", "versions", "summary", "progress", "library",
    "search", "context", "trash", "conflicts", "rag", "assets", "jobs", "style-rules",
  ]) {
    void queryClient.invalidateQueries({ queryKey: ['project-todos', projectId] });
    void queryClient.invalidateQueries({ queryKey: ['quality-coverage', projectId] });
    void queryClient.invalidateQueries({ queryKey: ['writing-goals', projectId] });
    await Promise.all(
      keys.map((key) =>
        key === 'library' ? refreshLibrary(queryClient, projectId) : queryClient.invalidateQueries({ queryKey:
          ["chapter", "versions", "summary", "jobs"].includes(key)
            ? [key, projectId, chapterId] : [key, projectId] }),
      ),
    );
  }
  async function save(data: {
    content: string;
    contract: Record<string, unknown>;
    revision?: number;
  }) {
    if (!chapterId) return;
    requireChapterSource();
    beginWorking();
    try {
      const saved = await client.saveChapter(chapterId, data);
      if (editSession.active && hasChapterSource()) setNotice("工作副本已保存");
      await refreshWriting();
      if (editSession.active && hasChapterSource()) return saved;
    } finally {
      endWorking();
    }
  }
  async function complete(data: {
    content: string;
    contract: Record<string, unknown>;
    revision?: number;
  }) {
    if (!chapterId) return;
    requireChapterSource();
    beginWorking();
    try {
      const saved = await client.saveChapter(chapterId, data);
      if (!editSession.active) return;
      requireChapterSource();
      const item = prepareSubmission(projectId, chapterId, { expected_revision: saved.revision }, 'summary');
      await deliver(item);
      const current = await client.chapter(chapterId);
      queryClient.setQueryData(['chapter', projectId, chapterId], current);
      if (editSession.active && hasChapterSource()) {
        setNotice('正文已保存，总结任务已提交。可离开页面，稍后查看结果。');
        return current;
      }
    } finally {
      await refreshWriting();
      endWorking();
    }
  }
  async function decideAlert(
    id: string,
    decision: "accept" | "dismiss",
    confirmed: boolean,
  ) {
    await client.decideAlert(id, decision, confirmed);
    await refreshWriting();
  }
  function navigate(next: View) {
    if (next === view) return true;
    if (requestPending()) {
      setNotice("请等待当前请求结束后再切换工作区。");
      return false;
    }
    if (next === "quality" && (dirty || editSession.manuscriptDirty || editSession.summaryDirty)) {
      setNotice("请先保存正文和总结，再进入质量优化。");
      return false;
    }
    if (
      dirty &&
      (view === "write" || view === "ai") &&
      !window.confirm("本章有未保存修改。确定离开正文？")
    )
      return false;
    clearDirty();
    if (next === 'quality') { setQualityTargetRunId(undefined); setFreshQualityReview(0); }
    setView(next);
    setTrashOpen(false);
    return true;
  }
  function selectNode(id: string) {
    const destination: View = id === "project-chat" || workspace?.nodes.find(node => node.id === id)?.kind === "chapter"
      ? "ai" : "story";
    if (view === destination && (id === selectedNodeId || (destination === "ai" && id === chapterId))) return;
    if (requestPending()) {
      setNotice("请等待当前请求结束后再切换章节。");
      return;
    }
    if (dirty && !window.confirm("本章有未保存修改。确定切换章节？")) return;
    clearDirty();
    setSelectedNodeId(id);
    if (id === "project-chat") {
      setView("ai");
      setJob(null);
      setManuscriptOpen(false);
      return;
    }
    setView(destination);
    setJob(null);
  }
  function openManuscript(id: string) {
    if (requestPending()) { setNotice('请等待当前请求结束后再打开章节。'); return false; }
    if (!hasChapterSource(id)) { setNotice('此章节已删除或不在当前目录中，请先恢复章节或刷新工作区；本机草稿仍可复制。'); return false; }
    if (dirty && !window.confirm('本章有未保存修改。确定打开另一章节？')) return false;
    clearDirty(); setSelectedNodeId(id); setView('write'); setJob(null); setTrashOpen(false); setManuscriptOpen(true);
    return true;
  }
  async function openTodo(item: ProjectTodo) {
    if (requestPending()) throw Error('请等待当前请求结束后再打开待办。');
    if (dirty || editSession.manuscriptDirty || editSession.summaryDirty) throw Error('请先保存正文和总结，再打开待办。');
    const generation = ++todoRequest.current;
    if (item.chapter_id && !workspace?.nodes.some(node => node.id === item.chapter_id))
      throw Error('章节已从当前目录移除，请刷新工作区后再试。');
    const thread = item.destination === 'ai' && item.conversation_id ? await conversations.detail(item.conversation_id) : undefined;
    if (!mounted.current || !editSession.active || generation !== todoRequest.current) return;
    if (requestPending() || editSession.manuscriptDirty || editSession.summaryDirty)
      throw Error('读取待办期间的新输入已保留，请先保存后再跳转。');
    if (item.chapter_id) requireChapterSource(item.chapter_id);
    if (thread && (thread.status !== 'active' || (thread.chapter_id ?? null) !== item.chapter_id))
      throw Error('此任务的会话已归档、删除或不属于目标章节，请先恢复会话。');
    const scope = item.chapter_id ?? 'project';
    if (item.destination === 'ai') {
      if (thread) {
        queryClient.setQueryData(['conversation', projectId, thread.id], thread);
        setConversationSelections(current => ({ ...current, [scope]: thread.id }));
        saveWorkspaceConversation(projectId, scope, thread.id);
      }
      if (item.job_id) {
        const targetScope = `${projectId}:${scope}${thread && !thread.is_default ? `:${thread.id}` : ''}`;
        setJobSelection({ scope: targetScope, id: item.job_id });
      }
    }
    clearDirty(); setTrashOpen(false); setJob(null);
    if (item.destination === 'quality') { setQualityTargetRunId(item.job_id ?? undefined); setFreshQualityReview(0); }
    setSelectedNodeId(item.chapter_id ?? 'project-chat'); setView(item.destination);
    if (item.destination === 'write') setManuscriptOpen(true);
    setTodosOpen(false);
  }
  function openCoverage(item: CoverageAction) {
    if (requestPending()) throw Error('请等待当前请求结束后再打开审校。');
    if (dirty || editSession.manuscriptDirty || editSession.summaryDirty)
      throw Error('请先保存正文和总结，再打开审校。');
    requireChapterSource(item.chapter_id);
    setSelectedNodeId(item.chapter_id);
    setQualityTargetRunId(item.action === 'report' ? item.job_id : undefined);
    setFreshQualityReview(current => item.action === 'review' ? current + 1 : 0);
    setView('quality'); setCoverageOpen(false); setTrashOpen(false); setJob(null);
  }
  async function openReference(item: MaterialReference) {
    const generation = ++selectionGeneration.current;
    setSelected(null);
    setEditing(undefined);
    setDetailTarget(item);
    setDetailPending(true);
    setDetailError('');
    setReferenceRequest((n) => n + 1);
    try {
      const result = await queryClient.fetchQuery({ queryKey: ['library', projectId, 'detail', item.type, item.id],
        queryFn: () => client.materialDetail(item), staleTime: 0 });
      if (mounted.current && selectionGeneration.current === generation) setSelected(result);
    } catch (error) {
      if (mounted.current && selectionGeneration.current === generation) setDetailError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mounted.current && selectionGeneration.current === generation) setDetailPending(false);
    }
  }
  function closeReference() {
    selectionGeneration.current++;
    setDetailTarget(null);
    setSelected(null);
    setEditing(undefined);
    setDetailPending(false);
    setDetailError('');
  }
  function createMaterial() {
    selectionGeneration.current++;
    setEditing(null);
  }
  if (workspaceQuery.error && !workspace)
    return (
      <main className="error-page">
        <h1>项目无法打开</h1>
        <DiagnosticError error={workspaceQuery.error} />
        <button onClick={() => workspaceQuery.refetch()}>重试</button>
      </main>
    );
  if (!workspace || !project)
    return <div className="app-loading">正在打开独立资料库…</div>;
  const defaultContext = (selectedJobQuery.data ?? job)?.context_snapshot ?? {
    token_budget: Number(displayedInputBudget) || 0,
    total_estimated_tokens: 0,
    fragments: [],
  };
  const viewQueries = { library: libraryView, ideas: ideasView, story: storyView,
    entities: entitiesView, knowledge: knowledgeView, style: styleView };
  const activeView = view in viewQueries ? viewQueries[view as keyof typeof viewQueries] : undefined;
  const content = (() => {
    if (activeView && !activeView.data) return activeView.error
      ? <div>资料工作区加载失败：<DiagnosticError error={activeView.error} /><button onClick={() => activeView.refetch()}>重试资料工作区</button></div>
      : <p role="status">正在加载资料工作区…</p>;
    if (view === "wiki") return <WikiWorkspace projectId={projectId} chapters={chapterNodes}
      onOpen={openReference} onOpenChapter={selectNode} />;
    if (view === "quality") return <>
      <div className="quality-navigation"><button onClick={() => navigate("ai")}>返回写作</button></div>
      <QualityWorkspace key={qualityTargetRunId ?? `quality-${freshQualityReview}`} initialRunId={qualityTargetRunId} startNew={freshQualityReview > 0}
        projectId={projectId} chapters={chapterNodes} initialChapterId={chapterId}
        sourceUnavailable={sourceUnavailable}
        beforeStart={() => {
          if (requestPending()) throw Error("请等待当前编辑请求结束后再开始质量优化。");
          if (dirty || editSession.manuscriptDirty || editSession.summaryDirty)
            throw Error("请先保存正文和总结，再开始质量优化。");
          if (!modelQuery.data || modelQuery.error) throw Error("模型配置尚未确认，请在设置中检查后重试。");
          return true;
        }}
        beforeAccept={id => {
          requireChapterSource(id);
          if (requestPending()) throw Error("请等待当前编辑请求结束后再采纳优化稿。");
          if (dirty || editSession.manuscriptDirty || editSession.summaryDirty)
            throw Error("请先保存正文和总结，再采纳优化稿。");
          return true;
        }}
        onAccepted={async id => {
          await Promise.all(["chapter", "versions", "summary"].map(key =>
            queryClient.invalidateQueries({ queryKey: [key, projectId, id] })));
          await refreshWriting(["workspace", "progress", "library", "search", "context", "conflicts", "rag"]);
          setNotice(<><span>采纳的正文已保存。确认章节后，可继续生成总结并更新连续性记录。</span>
            <button onClick={() => openManuscript(id)}>打开章节，完成本章</button></>);
        }} onOpenChapter={openManuscript} />
    </>;
    if (view === "library")
      return (
        <section className="library-page">
          <header className="section-heading">
            <div>
              <span className="eyebrow">只属于这一个故事</span>
              <h1>项目素材库</h1>
            </div>
            <button className="primary-action" onClick={createMaterial}>
              新建素材
            </button>
          </header>
          <div className="wiki-library-entry">
            <div><strong>小说 Wiki</strong><p>按名称与别名查找人物、设定和情节，沿着来源读懂故事。</p></div>
            <button onClick={() => navigate("wiki")}>打开小说 Wiki</button>
          </div>
          {libraryView.data!.memory_conflicts.map((issue, index) => <p role="status" className="error-note" key={index}>
            {issue.message}（{issue.ids.join('、')}）
          </p>)}
          <p className="subtle">
            人物、世界、伏笔与连续性记录，共 {libraryTotal} 项（已加载 {libraryItems.length} 项）。
          </p>
          <div className="library-cards">
            {libraryItems.map((item) => (
              <button
                className="library-card"
                key={item.type + item.id}
                onClick={() => openReference(item)}
              >
                <span className="badge">{categoryNames[item.type]}</span>
                <h3>{item.title}</h3>
                <p>{item.preview || "打开补充设定"}</p>
                <small>
                  修订 {item.revision} {item.is_pinned ? "· 已固定" : ""}
                </small>
              </button>
            ))}
          </div>
          {libraryQuery.isFetching && <p role="status">正在加载素材…</p>}
          {libraryQuery.hasNextPage && <button disabled={libraryQuery.isFetchingNextPage} onClick={() => libraryQuery.fetchNextPage()}>加载更多素材</button>}
        </section>
      );
    if (view === "ideas")
      return (
        <IdeaBoard
          ideas={ideasView.data!.ideas}
          onCreate={async (idea) => {
            await client.createIdea(idea);
            await refreshWriting();
          }}
        />
      );
    if (view === "story")
      return (
        <StoryMap
          nodes={storyView.data!.nodes}
          plots={storyView.data!.plots}
          selectedNodeId={selectedNodeId}
          onSelectNode={setSelectedNodeId}
          onCreateNode={async (node) => {
            await client.createNode(node);
            await refreshWriting();
          }}
          onCreatePlot={async (plot) => {
            await client.createPlot(plot);
            await refreshWriting();
          }}
        />
      );
    if (view === "entities")
      return (
        <EntityStudio
          entities={entitiesView.data!.entities}
          onCreate={async (entity) => {
            await client.createEntity(entity);
            await refreshWriting();
          }}
        />
      );
    if (view === "knowledge")
      return (
        <KnowledgeBase
          canonFacts={knowledgeView.data!.canon_facts}
          timeline={knowledgeView.data!.timeline}
          onCreateCanon={async (fact) => {
            await client.createCanon(fact);
            await refreshWriting();
          }}
          onCreateTimeline={async (event) => {
            await client.createTimeline(event);
            await refreshWriting();
          }}
        />
      );
    if (view === "settings") return <><ModelSettings /><SpendingPanel projectId={projectId} /><AutomaticBackups key={projectId} projectId={projectId} /></>;
    if (view === "preparation")
      return <ProjectPreparation projectId={projectId} client={client} />;
    if (view === "conflicts")
      return (
        <>
          <ConflictCenter
            conflicts={openConflicts}
            onDecide={async (id, optionId, note) => {
              await client.decideConflict(id, optionId, note);
              await queryClient.invalidateQueries({
                queryKey: ["conflicts", projectId],
              });
            }}
            onResolveEntityState={async (id, stateId, revision, note) => {
              await client.resolveEntityStateConflict(id, stateId, revision, note);
              await queryClient.invalidateQueries({
                queryKey: ["conflicts", projectId],
              });
            }}
          />
          {!openConflicts.length && (
            <p className="empty-note wide">
              当前没有待处理冲突。运行连续性检查后，问题会在这里形成可选择的解决分支。
            </p>
          )}
        </>
      );
    if (view === "style")
      return (
        <StyleLab
          candidates={preferencesQuery.data ?? []}
          rules={styleRulesQuery.data?.rules ?? []}
          profiles={styleView.data!.styles}
          conflicts={styleView.data!.memory_conflicts}
          onUpdateProfile={async (profile, isActive) => {
            try {
              await client.editMaterial({ type: 'style', id: profile.id }, {
                revision: profile.revision, fields: { is_active: isActive },
              });
            } finally { await refreshWriting(); }
          }}
          onPinProfile={async (profile, isPinned) => {
            try { await client.editMaterial({ type: 'style', id: profile.id }, { revision: profile.revision, is_pinned: isPinned }); }
            finally { await refreshWriting(); }
          }}
          onCreateProfile={async (profile) => {
            await client.createStyle(profile);
            await refreshWriting();
          }}
          onConfirm={async (id, instruction) => {
            await client.confirmPreference(id, instruction);
            await refreshWriting(["preferences", "style-rules", "library", "search", "rag"]);
          }}
          onDisable={async (id) => {
            await client.disablePreference(id);
            await refreshWriting(["preferences", "style-rules", "library", "search", "rag"]);
          }}
        />
      );
    if (view === "assets")
      return (
        <AssetLibrary
          assets={assetsQuery.data ?? []}
          onGenerate={async (asset) => {
            await client.generateAsset({ ...asset, project_id: projectId });
            await refreshWriting();
          }}
        />
      );
    return renderManuscript();
  })();
  function renderManuscript() {
    if (chapterQuery.error && !chapterQuery.data)
      return (
        <section className="empty-chapter">
          <h2>正文加载失败</h2>
          <DiagnosticError error={chapterQuery.error} />
          <button onClick={() => chapterQuery.refetch()}>重试加载</button>
        </section>
      );
    if (chapterId && chapterQuery.isLoading)
      return <p className="empty-note">正在读取本章正文…</p>;
    if (!chapterId || !chapterQuery.data)
      return (
        <section className="empty-chapter">
          <h2>先在故事地图中建立章节</h2>
          <button onClick={() => setView("story")}>打开故事地图</button>
        </section>
      );
    return (
      <>
        {chapterQuery.error && (
          <div>正文刷新失败，当前编辑已保留：<DiagnosticError error={chapterQuery.error} />
            <button onClick={() => chapterQuery.refetch()}>重试正文刷新</button>
          </div>
        )}
        <ChapterWorkspace
          key={chapterId}
          projectId={projectId}
          chapterId={chapterId}
          title={chapterNode?.title ?? "未命名章节"}
          document={chapterQuery.data}
          saving={working}
          sourceUnavailable={sourceUnavailable}
          onDirtyChange={manuscriptDirtyChange}
          onDraftChange={draftChange}
          replacement={replacement?.chapterId === chapterId ? replacement : undefined}
          onSave={save}
          onComplete={chapterQuery.data.status === 'summary_pending'
            && summaryJobs.data?.some(blocksSummaryGeneration)
            ? undefined : complete}
        />
        {summaryJobs.data?.filter(task => task.status !== 'succeeded' || task.effects?.ledger_pending || task.replaces_job_id).map(task =>
          <JobStatus key={task.id} job={task} onCancel={() => cancelTask(task)}
            sourceUnavailable={sourceUnavailable}
            onResume={confirm => resumeTask(task, confirm)} onReplace={() => replaceTask(task)}
            replaceDisabled={working || dirty || summaryJobs.data?.some(other => other.id !== task.id
              && blocksSummaryGeneration(other))}
            onRepair={async () => {
              await client.repairLedger(task.chapter_id!);
              await Promise.all(['rag', 'summary', 'jobs'].map(key =>
                queryClient.invalidateQueries({ queryKey: [key, projectId] })));
            }} />)}
        <details className="manuscript-details">
          <summary>章节总结与连续性</summary>
          {visibleSummary && (
            <ChapterSummaryPanel
              key={chapterId}
              projectId={projectId}
              summary={visibleSummary}
              deleted={summaryQuery.data === null}
              saving={working || sourceUnavailable}
              onDirtyChange={summaryDirtyChange}
              candidates={memoryCandidates}
              candidatesLoading={memoryCandidatesQuery.isLoading}
              candidateError={memoryCandidateLoadError}
              candidateBusyId={candidatePending
                ? memoryCandidateMutation.variables?.id ?? "other" : null}
              hasMoreCandidates={!!memoryCandidatesQuery.hasNextPage}
              loadingMoreCandidates={memoryCandidatesQuery.isFetchingNextPage}
              onLoadMoreCandidates={() => {
                if (!memoryCandidatesQuery.hasNextPage || memoryCandidatesQuery.isFetchingNextPage) return;
                void memoryCandidatesQuery.fetchNextPage();
              }}
              onRetryCandidates={() => {
                if (memoryCandidatesQuery.isFetchNextPageError) void memoryCandidatesQuery.fetchNextPage();
                else void memoryCandidatesQuery.refetch();
              }}
              onEditCandidate={async (id, payload, evidence, revision) => {
                await runMemoryCandidateMutation({
                  action: "edit", id, payload, evidence, revision,
                  scope: { projectId, chapterId },
                });
              }}
              onConfirmCandidate={async (id, revision) => {
                await runMemoryCandidateMutation({
                  action: "confirm", id, revision,
                  scope: { projectId, chapterId },
                });
              }}
              onRejectCandidate={async (id, revision) => {
                await runMemoryCandidateMutation({
                  action: "reject", id, revision,
                  scope: { projectId, chapterId },
                });
              }}
              onDiscard={() => {
                summaryDirtyChange(false);
                setRetainedSummary(null);
              }}
              onSave={async (recap, revision, details, summaryId) => {
                requireChapterSource();
                beginWorking();
                try {
                  const saved = await client.editSummary(chapterId, recap, revision, details, summaryId);
                  await refreshWriting();
                  requireChapterSource();
                  return saved;
                } finally { endWorking(); }
              }}
            />
          )}
          <DriftAlertCenter
            alerts={(conflictsQuery.data ?? []).filter(
              (alert) => !alert.chapter_id || alert.chapter_id === chapterId,
            )}
            onDecide={decideAlert}
          />
        </details>
        <details className="manuscript-details">
          <summary>历史版本</summary>
          <VersionPanel
            key={chapterId}
            busy={working || sourceUnavailable}
              versions={versions}
              hasMore={!!versionsQuery.hasNextPage}
              initialLoading={versionsQuery.isLoading}
              loadError={versionLoadError}
              retrying={versionsQuery.isFetching}
              loadingMore={versionsQuery.isFetchingNextPage}
            onLoadMore={() => {
              if (!versionsQuery.hasNextPage || versionsQuery.isFetchingNextPage) return;
              void versionsQuery.fetchNextPage();
              }}
              onRetry={() => {
                if (versionsQuery.isFetchNextPageError) void versionsQuery.fetchNextPage();
                else void versionsQuery.refetch();
              }}
            onCompare={(from, to) =>
              client.compareVersions(chapterId, from, to)
            }
            onRestore={async (id) => {
              if (requestPending()) return;
              requireChapterSource();
              if (
                dirty &&
                !window.confirm("恢复版本会替换当前未保存草稿，确定继续？")
              )
                return;
              const revision = chapterQuery.data?.revision;
              if (revision === undefined) throw Error("正文修订号尚未加载，请刷新后重试。");
              const draft = editSession.draft;
              beginWorking();
              try {
                await client.restoreVersion(chapterId, id, revision);
                const document = await client.chapter(chapterId);
                queryClient.setQueryData(["chapter", projectId, chapterId], document);
                if (editSession.active && hasChapterSource()) {
                  setReplacement({ chapterId, document, draft });
                  setNotice(editSession.draft === draft ? "已创建恢复版本。" : "恢复期间的新输入已保留，请确认后保存草稿。");
                }
                await refreshWriting();
              } finally { endWorking(); }
            }}
          />
        </details>
      </>
    );
  }

  const isCreation = view === "ai" || view === "write";
  const editorGeneration = selectionGeneration.current;

  function beforeWritingSend(task: string): boolean | Promise<boolean> {
    if (!["draft", "continue", "full_chapter"].includes(task)) return true;
    const state = preparationQuery.data;
    if (!state || Array.isArray(state) || state.status === "completed") return true;
    const signature = `${state.status}:${state.revision}`;
    if (preparationBypass.current === signature) return true;
    return new Promise<boolean>((resolve) => {
      setPreparationDecision({ unresolved: state.unresolved_count, resolve });
    });
  }

  function closePreparationDecision(allowed: boolean) {
    const decision = preparationDecision;
    if (!decision) return;
    if (allowed && preparationQuery.data && !Array.isArray(preparationQuery.data)) {
      preparationBypass.current = `${preparationQuery.data.status}:${preparationQuery.data.revision}`;
    }
    setPreparationDecision(null);
    decision.resolve(allowed);
  }

  return (
    <FocusShell
      transitionKey={`${view}:${chapterId ?? "project"}:${trashOpen ? "trash" : "workspace"}`}
      referenceRequest={referenceRequest}
      onReferenceClose={closeReference}
      projectId={projectId}
      projectTitle={project.title}
      nodes={workspace.nodes}
      selectedNodeId={
        selectedNodeId === "project-chat" ? "project-chat" : (chapterId ?? null)
      }
      onSelectNode={selectNode}
      rail={<WorkspaceRail view={view} onChange={navigate} />}
      status={view !== "settings" && (
        <div
          className={`connection-strip ${modelQuery.data?.mode === "api" ? "is-external" : ""}`}
        >
          <span className="connection-dot" />
          <span>{modelQuery.error
            ? "模型配置不可用 · 请打开设置检查"
            : !modelQuery.data
              ? "正在确认模型状态…"
              : modelQuery.data.mode === "api"
                ? `外部 API · ${modelQuery.data.model} · 发送当前任务相关内容`
                : modelQuery.data.mode === "local"
                  ? `本机模型 · ${modelQuery.data.model}`
                  : "离线演示 · 回复为示例内容"}</span>
          <button onClick={() => navigate("settings")}>模型设置</button>
        </div>
      )}
      toolbar={
        <ProjectSwitcher
          projects={projects}
          activeId={projectId}
          onChange={(id) => {
            if (requestPending()) setNotice("请等待当前请求结束后再切换小说。");
            else onProjectChange(id);
          }}
          onCreate={() => {
            if (requestPending()) setNotice("请等待当前请求结束后再新建小说。");
            else onCreate();
          }}
        />
      }
      sidebar={
        <>
        <MaterialSidebar
          items={debounced ? (searchQuery.data?.items ?? []) : libraryItems}
          nodes={workspace.nodes}
          selectedNodeId={chapterId ?? null}
          onSelectNode={selectNode}
          onOpen={openReference}
          onCreate={createMaterial}
          onTrash={() => {
            if (navigate("library")) setTrashOpen(true);
          }}
          query={query}
          onQuery={setQuery}
          category={category}
          onCategory={setCategory}
          counts={libraryCounts}
          total={debounced ? (searchQuery.data?.total ?? 0) : libraryTotal}
          pinned={pinnedQuery.data?.items ?? []}
          loading={debounced ? searchQuery.isFetching : libraryQuery.isFetching}
          error={(debounced ? searchQuery.error : libraryQuery.error)?.message}
          onRetry={() => { void (debounced ? searchQuery.refetch() : libraryQuery.refetch()); }}
          hasMore={!debounced && !!libraryQuery.hasNextPage}
          onLoadMore={() => { void libraryQuery.fetchNextPage(); }}
        />
        {pinnedQuery.error && <p role="alert">固定素材加载失败：{pinnedQuery.error.message}<button onClick={() => pinnedQuery.refetch()}>重试固定素材</button></p>}
        <RagStatus projectId={projectId} />
        </>
      }
      inspector={
        <>
          <ContextLens
            fragments={defaultContext.fragments ?? []}
            references={ledgerQuery.data?.pages.flatMap(page => page.items) ?? []}
            selected={selected}
            onOpen={openReference}
            onClose={closeReference}
            hasSelection={!!detailTarget}
            detailPending={detailPending}
            detailError={detailError}
            onRetry={() => { if (detailTarget) void openReference(detailTarget); }}
            ledgerLoading={ledgerQuery.isFetching}
            ledgerError={ledgerQuery.error?.message}
            onRetryLedger={() => { void ledgerQuery.refetch(); }}
            ledgerHasMore={!!ledgerQuery.hasNextPage}
            onLoadMoreLedger={() => { void ledgerQuery.fetchNextPage(); }}
            onPin={async (item) => {
              const generation = selectionGeneration.current;
              try {
                const next = await client.editMaterial(item, {
                  revision: item.revision,
                  is_pinned: !item.is_pinned,
                });
                const detailKey = ['library', projectId, 'detail', next.type, next.id];
                await queryClient.cancelQueries({ queryKey: detailKey, exact: true });
                queryClient.setQueryData(detailKey, next);
                if (mounted.current && generation === selectionGeneration.current) setSelected(next);
                await refreshWriting();
              } catch (e) {
                setNotice(<DiagnosticError error={e} />);
              }
            }}
            onEdit={(item) => {
              if (item.type === "summary" || item.type === "manuscript") {
                selectNode(String(item.record.chapter_id));
                setManuscriptOpen(true);
                setNotice("在正文下方的连续性总结区域编辑。");
              } else setEditing(item);
            }}
          />
          {progressQuery.data && (
            <ProgressPulse progress={progressQuery.data} />
          )}
          <a
            className="export-link"
            href={`/api/v1/projects/${projectId}/export`}
          >
            导出项目备份
          </a>
        </>
      }
    >
      <div className="project-tool-links" aria-label="项目工具">
        <button onClick={() => setTodosOpen(true)}>创作待办</button>
        <button onClick={() => setCoverageOpen(true)}>整书审校</button>
        <button onClick={() => setGoalsOpen(true)}>创作目标</button>
        <button onClick={() => setDraftRecoveryOpen(true)}>恢复本机草稿</button>
        <button onClick={() => setExportOpen(true)}>导出正文</button>
        <button onClick={onRestore}>恢复项目备份</button>
      </div>
      {coverageOpen && <Modal title="整书审校" onClose={() => setCoverageOpen(false)}><QualityCoverage projectId={projectId} onOpen={openCoverage} /></Modal>}
      {goalsOpen && <Modal title="创作与交稿目标" onClose={() => setGoalsOpen(false)}><WritingGoals projectId={projectId} onSaved={async () => {
        await Promise.all(['progress', 'workspace'].map(key => queryClient.invalidateQueries({ queryKey: [key, projectId] })));
      }} /></Modal>}
      {todosOpen && <Modal title="创作待办" onClose={() => { todoRequest.current++; setTodosOpen(false); }}><ProjectTodos projectId={projectId} onOpen={openTodo} /></Modal>}
      {exportOpen && <Modal title="导出小说正文" onClose={() => setExportOpen(false)}><ManuscriptExport projectId={projectId} nodes={workspace.nodes} onClose={() => setExportOpen(false)} /></Modal>}
      {draftRecoveryOpen && <Modal title="本机草稿恢复箱" onClose={() => setDraftRecoveryOpen(false)}><LocalDraftRecovery projectId={projectId} onOpenChapter={id => { if (openManuscript(id)) setDraftRecoveryOpen(false); }} /></Modal>}
      {importAnalysisQuery.data && (
        <ImportAnalysisCard
          analysis={importAnalysisQuery.data}
          pendingCount={pendingImportCandidateCount}
          onOpen={() => setImportReviewOpen(true)}
        />
      )}
      {importAnalysisQuery.error && <p className="error-note">导入分析状态加载失败：{importAnalysisQuery.error.message}</p>}
      {importAnalysisMutation.error && <p role="alert" className="error-note">导入分析操作失败：{importAnalysisMutation.error.message}</p>}
      {importReviewOpen && (
        <Modal title="导入分析与记忆审核" onClose={() => setImportReviewOpen(false)}>
          {importAnalysisQuery.data && (
            <ImportAnalysisPanel
              analysis={importAnalysisQuery.data}
              busy={importAnalysisMutation.isPending}
              onPause={(revision) => importAnalysisMutation.mutateAsync({ action: "pause", revision })}
              onContinue={(revision) => importAnalysisMutation.mutateAsync({ action: "continue", revision })}
              onRetry={(revision) => importAnalysisMutation.mutateAsync({ action: "retry", revision })}
            />
          )}
          {importCandidateQueryError && <p role="alert">待确认记忆加载失败：{importCandidateQueryError.message}<button onClick={() => {
            void pendingImportCandidatesQuery.refetch();
            void conflictImportCandidatesQuery.refetch();
          }}>重试</button></p>}
          {(pendingImportCandidatesQuery.isLoading || conflictImportCandidatesQuery.isLoading) && <p role="status">正在读取待确认记忆…</p>}
          <MemoryCandidateReview
            candidates={importCandidates}
            busyId={candidatePending ? importCandidateMutation.variables?.id ?? "other" : null}
            busyAction={importCandidateMutation.isPending ? importCandidateMutation.variables?.action : undefined}
            hasMore={hasMoreImportCandidates}
            onLoadMore={() => {
              if (pendingImportCandidatesQuery.hasNextPage) void pendingImportCandidatesQuery.fetchNextPage();
              if (conflictImportCandidatesQuery.hasNextPage) void conflictImportCandidatesQuery.fetchNextPage();
            }}
            onEdit={(id, revision, payload, evidence) => runImportCandidateMutation({ action: "edit", id, revision, payload, evidence })}
            onConfirm={(id, revision, resolution) => runImportCandidateMutation({ action: "confirm", id, revision, resolution })}
            onReject={(id, revision) => runImportCandidateMutation({ action: "reject", id, revision })}
            onBulkConfirm={(entries) => runImportCandidateMutation({ action: "bulk", id: "bulk", entries })}
          />
        </Modal>
      )}
      {preparationDecision && (
        <Modal title="正文生成前确认" onClose={() => closePreparationDecision(false)}>
          <div className="preparation-reminder">
            <p>
              {preparationDecision.unresolved
                ? `还有 ${preparationDecision.unresolved} 项高影响设定未整理。现在生成仍然可以，但越晚固定规则，后续返工范围可能越大。`
                : "创作准备尚未完成。现在生成仍然可以，后续也能回来补齐设定。"}
            </p>
            <div className="preparation-actions">
              <button onClick={() => {
                closePreparationDecision(false);
                navigate("preparation");
              }}>返回准备</button>
              <button className="primary-action" onClick={() => closePreparationDecision(true)}>仍然生成</button>
            </div>
          </div>
        </Modal>
      )}
      {!isCreation && view !== "settings" && view !== "quality" && (
        <div className="library-toolbar">
          <label>
            资料分类
            <select
              aria-label="资料工作区"
              value={view}
              onChange={(e) => navigate(e.target.value as View)}
            >
              {(
                [
                  ["library", "全部资料"],
                  ["wiki", "小说 Wiki"],
                  ["story", "大纲与章节"],
                  ["entities", "人物与世界"],
                  ["knowledge", "事实与时间线"],
                  ["ideas", "灵感"],
                  ["style", "写作风格"],
                  ["conflicts", "连续性问题"],
                  ["assets", "图片素材"],
                ] as const
              ).map(([id, label]) => (
                <option value={id} key={id}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={() => {
              setTrashOpen(!trashOpen);
            }}
          >
            回收站
          </button>
          <a href={`/api/v1/projects/${projectId}/export`}>导出备份</a>
        </div>
      )}
      {workspaceQuery.error && (
        <p role="alert">工作区刷新失败，当前编辑已保留：{workspaceQuery.error.message}
          <button onClick={() => workspaceQuery.refetch()}>重试工作区刷新</button>
        </p>
      )}
      {sourceUnavailable && (
        <p role="alert">当前章节已从导航中移除或暂时不可用。正文、章节约束与总结草稿已保留，可继续编辑和复制；恢复章节前不能保存或提交 AI 任务。切换章节或工作区前请确认放弃本地修改。</p>
      )}
      {!trashOpen && activeView?.data && activeView.error && (
        <p role="alert">资料工作区刷新失败，已有资料与输入已保留：{activeView.error.message}
          <button onClick={() => activeView.refetch()}>重试资料工作区刷新</button>
        </p>
      )}
      {notice && (
        <div role="status" className="notice">
          {notice}
          <button
            className="icon-button"
            aria-label="关闭提示"
            onClick={() => setNotice("")}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      )}
      {libraryQuery.error && (
        <p role="alert">素材加载失败：{libraryQuery.error.message}<button onClick={() => libraryQuery.refetch()}>重试素材列表</button></p>
      )}
      {searchQuery.error && (
        <p role="alert">搜索失败：{searchQuery.error.message}<button onClick={() => searchQuery.refetch()}>重试搜索</button></p>
      )}
      {trashOpen ? (
        <TrashView
          items={trashQuery.data?.pages.flatMap(page => page.items) ?? []}
          loading={trashQuery.isFetching}
          loadError={trashQuery.error?.message}
          onRetry={() => { void trashQuery.refetch(); }}
          hasMore={!!trashQuery.hasNextPage}
          onLoadMore={() => { void trashQuery.fetchNextPage(); }}
          onRestore={async (item) => {
            const result = await client.restoreMaterial(item);
            const dependents = result.reactivated_dependents ?? [];
            setNotice(dependents.length ? <>
              <p>依赖实体已恢复，以下作者资料已重新加入检索/生成。</p>
              <ul aria-label="已重新启用的相关资料">
                {dependents.map(dependent => <li key={`${dependent.type}:${dependent.id}`}>
                  {categoryNames[dependent.type]} · {dependent.id}
                </li>)}
              </ul>
            </> : "已恢复");
            await refreshWriting();
          }}
          onPurge={async (item) => {
            await client.purgeMaterial(item);
            await refreshWriting();
          }}
        />
      ) : isCreation ? (
        <>
        <div className="quality-entry"><button onClick={() => navigate("quality")}>
          <Icon name="spark" />质量优化
        </button></div>
        <details className="conversation-management" onToggle={event => { if (event.currentTarget.open) setConversationPanelOpened(true); }}>
        <summary>会话与历史{conversationQuery.data?.title ? ` · ${conversationQuery.data.title}` : ''}</summary>
        {conversationPanelOpened && <ConversationsPanel projectId={projectId} chapterId={chapterId} selectedId={selectedConversationId}
          branchFromJobId={selectedJobSummary?.status === 'succeeded' ? selectedJobSummary.id : undefined}
          beforeChange={async () => { if (requestPending()) throw Error('请等待当前请求结束后再切换会话。'); return true; }}
          onSelect={selectConversation}
          onLifecycleChange={async thread => {
            if (thread.status !== 'active' && (thread.id === selectedConversationId || !selectedConversationId && thread.is_default)) {
              setConversationSelections(current => ({ ...current, [conversationScope]: thread.id }));
              saveWorkspaceConversation(projectId, conversationScope, thread.id);
              setJob(null);
            }
            await Promise.all([
            queryClient.invalidateQueries({ queryKey: ['conversation', projectId] }),
            queryClient.invalidateQueries({ queryKey: ['jobs', projectId] }),
            queryClient.invalidateQueries({ queryKey: ['project-todos', projectId] }),
          ]); }} />}
        </details>
        {view === "ai" && (!preparationQuery.data || !Array.isArray(preparationQuery.data)) && (
          <ProjectPreparation projectId={projectId} client={client} compact onOpenFull={() => navigate("preparation")} />
        )}
        <div
          className={`conversation-layout ${manuscriptOpen ? "with-manuscript" : ""}`}
        >
          {!knownJobSummary && selectedJobId && selectedJobQuery.isPending && <p role="status">正在恢复上次对话…</p>}
          {!knownJobSummary && !missingJob && selectedJobQuery.error && <p role="alert">上次对话读取失败：{selectedJobQuery.error.message}
            <button onClick={() => { void selectedJobQuery.refetch(); }}>重试读取</button></p>}
          {conversationUnavailable && <p role="status">{conversationQuery.isPending ? '正在恢复上次会话…' : '当前会话不可写入，请恢复会话或选择另一个会话。'}<button onClick={() => void conversationQuery.refetch()}>重新读取会话</button></p>}
          {!!conversationQuery.error && <DiagnosticError error={conversationQuery.error} />}
          {!conversationUnavailable && <ChatWorkspace
            key={`${chapterId ?? "project"}:${conversationId ?? 'default'}`}
            draftKey={`studio:chat-draft:${projectId}:${chapterId ?? "project"}`}
            chapterTitle={chapterId ? chapterNode?.title ?? "未命名章节" : "聊聊这个故事"}
            hasChapter={!!chapterId}
            jobs={visibleJobs}
            projectId={projectId}
            chapterId={chapterId}
            conversationId={conversationId}
            appliedStyles={selectedJobQuery.data ? (selectedJobQuery.data.context_snapshot.fragments ?? [])
              .filter(fragment => ['style_profile', 'style_rule'].includes(fragment.source_type))
              .map(fragment => styleView.data?.styles.find(style => style.id === fragment.source_id)?.name
                ?? `${fragment.source_type === 'style_profile' ? '文风方案' : '文风规则'} ${fragment.source_id.slice(0, 8)}`) : undefined}
            selectedJobId={selectedJobSummary?.id}
            selectedJob={selectedJobQuery.data}
            onSelectJob={id => setJobSelection({ scope: jobScope, id })}
            onLoadOlder={jobsQuery.hasNextPage ? () => jobsQuery.fetchNextPage() : undefined}
            loadingOlder={jobsQuery.isFetchingNextPage}
            detailLoading={selectedJobQuery.isFetching}
            detailError={selectedJobQuery.error}
            onRetryDetail={() => { void selectedJobQuery.refetch(); }}
            running={working || jobsQuery.isLoading}
            sourceUnavailable={sourceUnavailable}
            inputBudget={displayedInputBudget}
            inputBudgetAutomatic={!inputBudget.overridden}
            onFollowModel={() => {
              clearInputBudget(projectId);
              setInputBudget({ value: '', overridden: false });
            }}
            onInputBudgetChange={value => {
              setInputBudget({ value, overridden: true });
              saveInputBudget(projectId, value);
            }}
            disabledReason={
              sourceUnavailable ? "章节来源不可用，任务提交已禁用。"
                : modelQuery.error ? "模型配置不可用，请打开设置检查。"
                : !modelQuery.data ? "模型状态尚未确认" : modelBudgetError || undefined
            }
            onSettings={() => navigate("settings")}
            onFeedback={async (reply, payload) => {
              await client.feedback({ ...payload, job_id: reply.id, chapter_id: reply.chapter_id ?? null });
              await refreshWriting(["preferences"]);
              setNotice("评价已保存。偏好需在写作风格中确认后才会生效。");
            }}
            onOpenManuscript={() => {
              if (requestPending() && manuscriptOpen) {
                setNotice("请等待当前请求结束后再收起编辑区。");
                return;
              }
              if (manuscriptOpen && dirty) {
                setNotice("请先保存正文与总结，再收起编辑区。");
                return;
              }
              setManuscriptOpen(!manuscriptOpen);
            }}
            onSend={async (task, instructions) => {
              requireChapterSource();
              if (!modelQuery.data || modelQuery.error)
                throw new Error("模型状态尚未确认，请检查模型设置。");
              if (dirty || editSession.manuscriptDirty || editSession.summaryDirty)
                throw new Error("正文有未保存修改，请先保存，再交给 AI 处理。");
              const budget = newTaskInputBudget();
              beginWorking();
              try {
                if (chapterId && !chapterQuery.data) throw Error('请等正文加载完成。');
                const command = {
                  chapter_id: chapterId ?? null,
                  task_type: task,
                  instructions,
                  token_budget: budget,
                  expected_revision: chapterQuery.data?.revision ?? null,
                  ...(conversationId ? { conversation_id: conversationId } : {}),
                };
                await preflightWriting(command);
                const item = prepareSubmission(projectId, chapterId ?? null, command);
                const next = await deliver(item);
                if (editSession.active) {
                  setJob(next);
                  setJobSelection(null);
                }
              } finally {
                endWorking();
              }
            }}
            beforeSend={beforeWritingSend}
            onCancel={cancelTask}
            onResume={resumeTask}
            onReplace={replaceTask}
            replaceDisabled={dirty || working}
            onAccept={async (jobId) => {
              requireChapterSource();
              if (
                dirty &&
                !window.confirm("写入候选会替换未保存草稿，仍要继续吗？")
              )
                return;
              const draft = editSession.draft;
              beginWorking();
              try {
                try {
                  await client.acceptJob(jobId);
                } catch (error) {
                  if (
                    error instanceof ApiError &&
                    error.code === "SEVERE_CONFIRMATION_REQUIRED"
                  ) {
                    if (!window.confirm(error.message)) return;
                    requireChapterSource();
                    await client.acceptJob(jobId, true);
                  } else throw error;
                }
                if (chapterId) {
                  const document = await client.chapter(chapterId);
                  queryClient.setQueryData(["chapter", projectId, chapterId], document);
                  if (editSession.active && hasChapterSource()) setReplacement({ chapterId, document, draft });
                }
                await refreshWriting();
                if (editSession.active && hasChapterSource()) {
                  setManuscriptOpen(true);
                  // A server-applied draft also changes the fingerprint, but is clean against its new baseline.
                  const retainedInput = editSession.manuscriptDirty && editSession.draft !== draft;
                  setNotice(retainedInput ? "采纳期间的新输入已保留，请确认后保存草稿。" : "已写入正文，并保存为新版本。");
                }
              } finally {
                endWorking();
              }
            }}
          />}
          {pending.filter(item => !item.operation.startsWith('wiki') && !item.operation.startsWith('quality') && item.chapterId === (chapterId ?? null)).map(item =>
            <p role="status" key={item.idempotencyKey}>有一项请求尚未取得回执。
              <button disabled={working || sourceUnavailable} onClick={async () => {
                beginWorking();
                try { await deliver(item); } catch (e) {
                  if (!(item.operation === 'summary' && e instanceof ApiError && e.code === 'SUMMARY_TASK_ACTIVE'))
                    setNotice(<DiagnosticError error={e} />);
                }
                finally { endWorking(); }
              }}>用原请求确认提交</button>
            </p>)}
          {jobsQuery.error && (
            <p role="alert">
              对话加载失败：{jobsQuery.error.message}
              <button onClick={() => jobsQuery.refetch()}>重试</button>
            </p>
          )}
          {manuscriptOpen && (
            <aside className="manuscript-drawer" aria-label="正文编辑区">
              <div className="manuscript-drawer-toolbar">
                <span>正文</span>
                <button
                  onClick={() => {
                    if (requestPending()) {
                      setNotice("请等待当前请求结束后再收起编辑区。");
                      return;
                    }
                    if (dirty) {
                      setNotice("请先保存正文与总结，再收起编辑区。");
                      return;
                    }
                    setManuscriptOpen(false);
                  }}
                >
                  收起正文
                </button>
              </div>
              {content}
            </aside>
          )}
        </div>
        </>
      ) : (
        <div className="focus-tool-page">{content}</div>
      )}
      {editing !== undefined && (
        <MaterialEditorSheet
          key={editing?.id ?? "new"}
          item={editing}
          conflicts={libraryView.data?.memory_conflicts}
          conflictsLoading={libraryView.isFetching}
          conflictsError={libraryView.error?.message}
          onRetryConflicts={() => { void libraryView.refetch(); }}
          onClose={() => { if (mounted.current && editorGeneration === selectionGeneration.current) setEditing(undefined); }}
          onSave={async (kind, data) => {
            const generation = selectionGeneration.current;
            const result = editing
              ? await client.editMaterial(editing, data)
              : await client.createMaterial(kind, data);
            if (mounted.current && generation === selectionGeneration.current) {
              setSelected(result);
              setDetailTarget({ type: result.type, id: result.id });
            }
            await refreshWriting();
          }}
          onDelete={async () => {
            if (editing) {
              const generation = selectionGeneration.current;
              const result = await client.deleteMaterial(editing);
              const dependents = result.inactive_dependents ?? [];
              setNotice(dependents.length ? <>
                <p>{dependents.length} 条相关事实或关系已暂时退出生成与检索，恢复实体后会重新启用。</p>
                <p>依赖实体已删除，暂不可用于检索/生成；作者资料仍保留，恢复实体后会重新启用。</p>
                <ul aria-label="暂不可用但仍保留的相关资料">
                  {dependents.map(dependent => <li key={`${dependent.type}:${dependent.id}`}>
                    {categoryNames[dependent.type]} · {dependent.id}
                  </li>)}
                </ul>
              </> : "已移到回收站");
              if (mounted.current && generation === selectionGeneration.current) closeReference();
              await refreshWriting();
            }
          }}
        />
      )}
    </FocusShell>
  );
}
