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
  issue_number: number | null;
  issue_title: string;
  issue_body: string | null;
  base_commit: string;
  difficulty: "easy" | "medium" | "hard" | "expert" | "unknown";
  tags: string[];
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
  issue_number: number | null;
  issue_title: string;
}

export interface AgentRunMetricSummary {
  id: UUID;
  file_localization_score: number | null;
  patch_applied: boolean;
  tests_passed: boolean;
  baseline_tests_passed: boolean;
  post_patch_tests_passed: boolean;
  hidden_tests_passed: boolean | null;
  hidden_tests_run_count: number;
  hidden_tests_failed_count: number;
  issue_resolved: boolean;
  regression_detected: boolean;
  issue_specific_score: number;
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
  run_hidden_tests: boolean;
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
  baseline_tests_passed: boolean;
  post_patch_tests_passed: boolean;
  hidden_tests_passed: boolean | null;
  hidden_tests_run_count: number;
  hidden_tests_failed_count: number;
  issue_resolved: boolean;
  regression_detected: boolean;
  issue_specific_score: number;
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

export interface BenchmarkPackSummary {
  task_count: number;
  ready_task_count: number;
  repositories_represented: string[];
  difficulty_distribution: Record<string, number>;
  tags: string[];
}

export interface BenchmarkPack {
  id: UUID;
  name: string;
  slug: string;
  description: string | null;
  version: string;
  source: string | null;
  created_at: string;
  summary: BenchmarkPackSummary;
}

export interface AnalyticsSummary {
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  approved_patches: number;
  rejected_patches: number;
  patch_apply_rate: number;
  visible_test_pass_rate: number;
  hidden_test_pass_rate: number | null;
  issue_resolved_rate: number;
  regression_rate: number;
  average_file_localization_score: number;
  average_issue_specific_score: number;
  average_modified_files_count: number;
  average_unrelated_files_count: number;
  total_tokens: number;
  total_cost: number;
  average_cost_per_run: number;
  average_execution_time_seconds: number;
}

export interface RepositoryAnalytics extends AnalyticsSummary {
  repository_id: UUID;
  repository_owner: string;
  repository_name: string;
  repository_url: string;
}

export interface PackAnalytics extends AnalyticsSummary {
  benchmark_pack_id: UUID;
  pack_name: string;
  pack_slug: string;
  pack_version: string;
}

export interface ModelLeaderboardRow {
  model_provider: string;
  model_name: string;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  issue_resolved_rate: number;
  visible_test_pass_rate: number;
  hidden_test_pass_rate: number | null;
  average_file_localization_score: number;
  average_issue_specific_score: number;
  average_cost_per_run: number;
  average_tokens_per_run: number;
  average_execution_time_seconds: number;
  average_modified_files_count: number;
  average_unrelated_files_count: number;
  rank_by_issue_resolved: number;
  rank_by_cost: number;
  rank_by_speed: number;
  rank_by_localization: number;
  composite_score: number;
}

export interface ApiErrorPayload {
  detail?: unknown;
}
