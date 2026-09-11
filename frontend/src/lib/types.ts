export type StoryNodeKind = "volume" | "chapter" | "scene";

export interface StoryNode {
  id: string;
  kind: StoryNodeKind;
  title: string;
  status: string;
  parent_id?: string | null;
  summary?: string;
  order_index?: number;
  target_words?: number;
}

export interface Project {
  id: string;
  title: string;
  premise: string;
  genre: string;
  target_words: number;
  daily_goal: number;
  status: string;
}

export interface ProjectRead extends Project {
  created_at: string;
  updated_at: string;
}

export type PreparationStatus = "not_started" | "in_progress" | "completed" | "skipped";
export type PreparationQuestionStatus = "open" | "answered" | "deferred" | "not_applicable";
export type PreparationAnswerFormat = "short_text" | "long_text" | "ordered_list" | "choice";
export interface PreparationQuestion {
  id: string;
  fingerprint: string;
  round: 1 | 2;
  setting_key: string;
  category: string;
  question: string;
  rationale: string;
  impact_areas: string[];
  priority: "high" | "medium";
  answer_format: PreparationAnswerFormat;
  options: string[];
  source_ids: string[];
  origin: "template" | "ai";
  status: PreparationQuestionStatus;
  answer: string | string[] | null;
  canon_fact_id: string | null;
  updated_at: string | null;
}
export interface ProjectPreparationState {
  project_id: string;
  revision: number;
  status: PreparationStatus;
  round: 0 | 1 | 2;
  source_hash: string;
  questions: PreparationQuestion[];
  generation_job_ids: string[];
  actionable_job_id: string | null;
  impact_notice: { fact_ids: string[]; chapter_count: number; created_at: string } | null;
  answered_count: number;
  unresolved_count: number;
  unresolved_high_count: number;
}
export interface PreparationQuestionCommand {
  revision: number;
  action: "answer" | "defer" | "not_applicable";
  answer?: string | string[];
}

export interface StoryEntity {
  id: string;
  kind: "character" | "location" | "organization" | "item";
  name: string;
  summary: string;
  profile: Record<string, unknown>;
  state: Record<string, unknown>;
}

export interface PlotThread {
  id: string;
  kind: "main" | "subplot" | "character" | "foreshadowing";
  title: string;
  status: string;
  start_node_id?: string | null;
  due_node_id?: string | null;
}

export interface Idea {
  id: string;
  title: string;
  content: string;
  tags: string[];
  source: string;
  status: string;
}

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

export interface StyleProfile {
  id: string;
  revision: number;
  name: string;
  is_active: boolean;
  is_pinned?: boolean;
  config: Record<string, unknown>;
}

export interface MemoryConflict {
  code: string;
  ids: string[];
  fields?: string[];
  requires_author_decision: boolean;
  message: string;
}

/** JSON accepted by the bounded backend contracts; runtime enforces depth and collection limits. */
export type BoundedJson = string | number | boolean | null | BoundedJson[] | BoundedJsonObject;
export interface BoundedJsonObject { [key: string]: BoundedJson }

export type GeneratedMemoryCandidateKind = "canon" | "entity_state" | "timeline" | "plot";
export type ImportMemoryCandidateKind =
  | "entity" | "relation" | "canon" | "timeline" | "plot" | "node"
  | "node_update" | "style_rule" | "idea" | "entity_state" | "chapter_summary";
export type MemoryCandidateStatus = "pending" | "confirmed" | "rejected" | "conflict";
export interface MemoryEvidence {
  quote: string;
  start: number;
  end: number;
}
export type ImportMemoryEvidence = {
  quote?: string;
  start?: number;
  end?: number;
  relative_path?: string;
} & Record<string, BoundedJson | undefined>;

export interface CanonMemoryCandidatePayload {
  subject_entity_id?: string | null;
  predicate: string;
  value: BoundedJson;
  valid_from_node_id?: string | null;
  valid_to_node_id?: string | null;
}
export interface EntityStateMemoryCandidatePayload {
  entity_id: string;
  data: BoundedJsonObject;
  valid_from_node_id: string;
  valid_to_node_id?: string | null;
  legacy_transition?: "baseline" | "retire" | null;
}
export interface TimelineMemoryCandidatePayload {
  chapter_id?: string | null;
  title: string;
  story_time?: string;
  sort_key?: number;
  description?: string;
}
export interface PlotMemoryCandidatePayload {
  kind: string;
  title: string;
  promise?: string;
  start_node_id?: string | null;
  due_node_id?: string | null;
  payoff?: string;
}
export type GeneratedMemoryCandidatePayload =
  | CanonMemoryCandidatePayload
  | EntityStateMemoryCandidatePayload
  | TimelineMemoryCandidatePayload
  | PlotMemoryCandidatePayload;

interface GeneratedMemoryCandidateBase {
  origin?: "generated";
  id: string;
  project_id: string;
  chapter_id: string | null;
  source_job_id: string | null;
  source_summary_id: string | null;
  source_version_id: string;
  evidence: MemoryEvidence;
  identity_hash: string;
  status: "pending" | "confirmed" | "rejected";
  revision: number;
  promoted_record_id: string | null;
  promotion_fingerprint: string | null;
  created_at: string;
  updated_at: string;
}
export interface GeneratedMemoryCandidate extends GeneratedMemoryCandidateBase {
  kind: GeneratedMemoryCandidateKind;
  payload: BoundedJsonObject;
}

export interface ImportMemoryCandidate {
  origin: "import";
  id: string;
  project_id: string;
  import_batch_id: string;
  source_document_id: string;
  chapter_id: string | null;
  source_version_id: string | null;
  kind: ImportMemoryCandidateKind;
  payload: BoundedJsonObject;
  evidence: ImportMemoryEvidence[];
  source_hash: string;
  dedupe_key: string;
  analysis_identity: string | null;
  status: MemoryCandidateStatus;
  revision: number;
  conflict: BoundedJsonObject;
  promoted_type: string | null;
  promoted_record_id: string | null;
  promotion_fingerprint: string | null;
  created_at: string;
  updated_at: string;
}

export type GeneratedMemoryCandidateListItem = GeneratedMemoryCandidate & { origin: "generated" };
export type MemoryCandidate = GeneratedMemoryCandidateListItem | ImportMemoryCandidate;
export type MemoryCandidateMutationResult = GeneratedMemoryCandidate | ImportMemoryCandidate;
export type UnifiedMemoryCandidate = MemoryCandidate;

export interface MemoryCandidatePage {
  items: MemoryCandidate[];
  total: number;
  counts: Partial<Record<MemoryCandidateStatus, number>>;
  next_cursor: string | null;
}

export type AtLeastOne<T extends object> = {
  [Key in keyof T]-?: Required<Pick<T, Key>> & Partial<Omit<T, Key>>;
}[keyof T];
export type GeneratedMemoryCandidatePatchRequest = { revision: number } & AtLeastOne<{
  payload: BoundedJsonObject;
  evidence: MemoryEvidence;
}>;
export type ImportMemoryCandidatePatchRequest = { revision: number } & AtLeastOne<{
  kind: ImportMemoryCandidateKind;
  payload: BoundedJsonObject;
  evidence: ImportMemoryEvidence[];
}>;
export type MemoryCandidatePatchRequest =
  | GeneratedMemoryCandidatePatchRequest
  | ImportMemoryCandidatePatchRequest;
export interface MemoryCandidateDecisionRequest {
  revision: number;
  decision?: "confirm" | "reject";
  resolution?: "create_separate";
}
export interface MemoryCandidateBulkConfirmEntry {
  candidate_id: string;
  revision: number;
}
export interface MemoryCandidateBulkConfirmRequest {
  /** Backend accepts 1..100 entries. */
  entries: MemoryCandidateBulkConfirmEntry[];
}

export type ImportCategory = "manuscript" | "task" | "outline" | "world" | "character" | "style" | "other";
export type ImportSourceKind = "folder" | "zip";
export interface ImportLimits {
  max_files: number;
  max_file_bytes: number;
  max_total_bytes: number;
  max_compression_ratio: number;
}
export interface ImportFilePreview {
  relative_path: string;
  audit_id: string;
  category: ImportCategory;
  title: string;
  encoding: string;
  size_bytes: number;
  byte_hash: string;
  content_hash: string;
  selected: boolean;
  warning: string;
  content_preview: string;
}
export interface ImportChapterPreview {
  source_document_id: string | null;
  draft_chapter_id: string;
  relative_path: string;
  title: string;
  order_index: number;
  existing_chapter_id: string | null;
  content_preview: string;
}
export interface ImportContinuation {
  confirmed: boolean;
  completed_through_node_id: string | null;
  current_chapter_id: string | null;
  objective: string;
  source_document_ids: string[];
  revision: number;
}
export interface ImportDraft {
  draft_id: string;
  title: string;
  source_kind: ImportSourceKind;
  manifest: BoundedJsonObject;
  files: ImportFilePreview[];
  chapters: ImportChapterPreview[];
  continuation: ImportContinuation;
  objective: string;
  revision: number;
}
export interface ImportDraftPatch {
  revision: number;
  title?: string;
  files?: ImportFilePreview[];
  chapters?: ImportChapterPreview[];
  continuation?: ImportContinuation;
  objective?: string;
}
export interface ImportCommitRequest {
  expected_revision: number;
}
export interface ImportCommitResponse {
  project: ProjectRead;
  import_batch_id: string;
}
export type ImportBatchStatus = "imported" | "analyzing" | "paused" | "partially_analyzed" | "analyzed" | "analysis_failed";
export interface ImportBatch {
  id: string;
  project_id: string;
  source_kind: ImportSourceKind;
  manifest: BoundedJsonObject;
  status: ImportBatchStatus;
  completed_units: number;
  total_units: number;
  last_error: string;
  revision: number;
  created_at: string;
  updated_at: string;
}
export type ImportAnalysisUnitStatus = "queued" | "running" | "succeeded" | "failed";
export type ImportAnalysisUnitKind = string;
export interface ImportAnalysisUnit {
  id: string;
  project_id: string;
  import_batch_id: string;
  source_document_id: string | null;
  chapter_id: string | null;
  chunk_key: string | null;
  unit_key: string;
  kind: ImportAnalysisUnitKind;
  source_hash: string;
  status: ImportAnalysisUnitStatus;
  attempt_count: number;
  worker_epoch: string | null;
  error_code: string | null;
  error_message: string;
  result: BoundedJsonObject;
  created_at: string;
  updated_at: string;
}
export interface ImportAnalysisProgress {
  total: number;
  completed: number;
  failed: number;
  queued: number;
  running: number;
}
export interface ImportAnalysisStatus {
  id: string;
  project_id: string;
  status: ImportBatchStatus;
  revision: number;
  progress: ImportAnalysisProgress;
  current_unit: ImportAnalysisUnit | null;
  last_error: string;
  created_at: string;
  updated_at: string;
}
export type ImportAnalysis = ImportAnalysisStatus;
export interface ImportAnalysisAction {
  revision: number;
  adopt_current_provider?: boolean;
}

export interface ChapterDocument {
  revision?: number;
  status?: string;
  content: string;
  contract: Record<string, unknown>;
  current_version_id: string | null;
}

export interface MaterialReference {
  id: string;
  type: string;
}
export interface LibrarySummary extends MaterialReference {
  id: string;
  type: string;
  title: string;
  preview: string;
  revision: number;
  is_pinned: boolean;
  deleted_at: string | null;
  purge_after: string | null;
  status?: string | null;
  origin?: string | null;
  channels?: string[];
  reason?: string;
  score?: number;
  constraint?: "hard" | "soft";
}
export interface LibraryItem extends LibrarySummary {
  content: string;
  record: Record<string, unknown>;
}
export interface InactiveDependent {
  type: "canon" | "relation";
  id: string;
  reason: "entity_deleted";
}
export interface ReactivatedDependent {
  type: "canon" | "relation";
  id: string;
}
export interface MaterialLifecycleResult extends LibraryItem {
  inactive_dependents?: InactiveDependent[];
  reactivated_dependents?: ReactivatedDependent[];
}
export type VectorState = "ready" | "needs_rebuild" | "disabled" | "degraded";
export interface RagHealth {
  vectors: VectorState;
  documents: number;
  ledger_pending?: boolean;
}
export interface LibraryPage {
  items: LibrarySummary[];
  total: number;
  counts: Record<string, number>;
  next_cursor: string | null;
}
export interface SummaryEvidence {
  field: string;
  index: number;
  quote: string;
  start: number;
  end: number;
}
export interface ContentCheckMetadata {
  version: number;
  context_hash?: string;
  findings: BoundedJson[];
  observations: BoundedJson[];
}
export interface ChapterSummary {
  id: string;
  chapter_id: string;
  version_id: string;
  title: string;
  recap: string;
  details: Record<string, string | string[] | SummaryEvidence[] | ContentCheckMetadata>;
  origin: string;
  provider: string;
  status: string;
  revision: number;
  content_hash: string;
  ledger_pending?: boolean;
}
export interface ContextFragment {
  source_type: string;
  source_id: string;
  reason: string;
  hard: boolean;
  estimated_tokens: number;
  content?: string;
  channel?: string;
  score?: number;
  citation?: { type: string; id: string; title: string };
}

export interface ChapterVersion {
  id: string;
  summary: string;
  source: string;
  word_count: number;
  created_at: string;
  parent_version_id?: string | null;
}
