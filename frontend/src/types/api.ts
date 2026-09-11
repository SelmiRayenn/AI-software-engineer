export type UUID = string;

export type FailureCategory =
  | "task_not_ready"
  | "repository_checkout_failed"
  | "docker_unavailable"
  | "setup_failed"
  | "baseline_tests_failed"
  | "model_provider_error"
  | "malformed_tool_call"
  | "unknown_tool"
  | "tool_error_limit_reached"
  | "patch_generation_failed"
  | "patch_apply_failed"
  | "patch_quality_blocked"
  | "post_patch_tests_failed"
  | "max_steps_reached"
  | "max_repair_attempts_reached"
  | "timeout"
  | "cancelled"
  | "unknown";

export interface Repository {
  id: UUID;
  name: string;
  owner: string;
  url: string;
  default_branch: string;
  language: string | null;
  created_at: string;
}

export interface BenchmarkTask {
  id: UUID;
  repository_id: UUID;
  repository: Repository;
  issue_number: number;
  issue_title: string;
  issue_body: string | null;
  base_commit: string;
  status: string;
  created_at: string;
}

export interface AgentRun {
  id: UUID;
  benchmark_task_id: UUID;
  model_provider: string;
  model_name: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  patch_review_status: string | null;
  failure_category: FailureCategory | null;
  failure_summary: string | null;
}

export interface AgentRunDetailRepository {
  name: string;
  owner: string;
  url: string;
}

export interface AgentRunDetailBenchmarkTask {
  id: UUID;
  issue_number: number;
  issue_title: string;
}

export interface AgentRunMetricSummary {
  id: UUID;
  file_localization_score: number | null;
  patch_applied: boolean;
  tests_passed: boolean;
  modified_files_count: number;
  unrelated_files_count: number;
  tokens_used: number | null;
  estimated_cost: number | null;
  execution_time_seconds: number | null;
  created_at: string;
}

export interface AgentRunDetail {
  id: UUID;
  status: string;
  benchmark_task_id: UUID;
  benchmark_task: AgentRunDetailBenchmarkTask;
  repository: AgentRunDetailRepository;
  model_provider: string;
  model_name: string;
  started_at: string;
  completed_at: string | null;
  review_status: string | null;
  changed_files: string[];
  metric_summary: AgentRunMetricSummary | null;
  run_config: AgentRunConfig | null;
  prompt_preview: AgentPromptPreview | null;
  failure_category: FailureCategory | null;
  failure_summary: string | null;
}

export interface AgentRunConfig {
  model_provider: string;
  model_name: string | null;
  max_steps: number;
  max_tool_errors: number;
  command_timeout_seconds: number;
  include_issue_comments: boolean;
  enable_test_tool: boolean;
  run_mode: "scripted" | "tool_loop";
}

export interface AgentPromptPreview {
  system_prompt: string;
  developer_safety_prompt: string;
  issue_context_prompt: string;
  tool_use_instructions: string;
  patch_submission_instructions: string;
}

export interface PatchSizeStats {
  size_bytes: number;
  changed_files_count: number;
  additions: number;
  deletions: number;
}

export interface GeneratedPatch {
  id: UUID;
  agent_run_id: UUID;
  patch_text: string;
  changed_files: string[];
  stats: PatchSizeStats;
  review_status: string;
  created_at: string;
}

export interface TestResult {
  id: UUID;
  agent_run_id: UUID;
  phase: string;
  command: string;
  passed: boolean;
  exit_code: number;
  stdout: string | null;
  stderr: string | null;
  duration_seconds: number | null;
  created_at: string;
}

export interface EvaluationMetric {
  id: UUID;
  agent_run_id: UUID;
  file_localization_score: number | null;
  patch_applied: boolean;
  tests_passed: boolean;
  modified_files_count: number;
  unrelated_files_count: number;
  tokens_used: number | null;
  estimated_cost: number | null;
  execution_time_seconds: number | null;
  created_at: string;
}

export interface HumanReview {
  id: UUID;
  generated_patch_id: UUID;
  decision: string;
  reviewer_name: string | null;
  review_notes: string | null;
  reviewed_at: string;
}

export interface PatchReview {
  generated_patch_id: UUID;
  review_status: string;
  export_eligible: boolean;
  review: HumanReview | null;
}

export interface PatchReviewRequest {
  reviewer_name: string;
  review_notes?: string | null;
  update_existing?: boolean;
}

export interface ApiErrorPayload {
  detail?: unknown;
}
