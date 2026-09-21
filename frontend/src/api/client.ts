import { apiBaseUrl } from "./config";
import type {
  AgentRun,
  AgentRunDetail,
  AnalyticsSummary,
  ApiErrorPayload,
  BenchmarkPack,
  BenchmarkTask,
  EvaluationMetric,
  GeneratedPatch,
  ModelLeaderboardRow,
  PackAnalytics,
  PatchReview,
  PatchReviewRequest,
  Repository,
  RepositoryAnalytics,
  TestResult,
  UUID,
} from "../types/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly payload: ApiErrorPayload | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function listRepositories(): Promise<Repository[]> {
  return requestJson<Repository[]>("/api/v1/repositories");
}

export interface BenchmarkTaskFilters {
  difficulty?: string;
  tag?: string;
}

export async function listBenchmarkTasks(
  filters: BenchmarkTaskFilters = {},
): Promise<BenchmarkTask[]> {
  const params = new URLSearchParams();
  if (filters.difficulty) params.set("difficulty", filters.difficulty);
  if (filters.tag) params.set("tag", filters.tag);
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<BenchmarkTask[]>(`/api/v1/benchmark-tasks${query}`);
}

export async function listBenchmarkTaskTags(): Promise<string[]> {
  return requestJson<string[]>("/benchmark-tasks/tags");
}

export async function listAgentRuns(): Promise<AgentRun[]> {
  return requestJson<AgentRun[]>("/api/v1/agent-runs");
}

export async function listBenchmarkPacks(): Promise<BenchmarkPack[]> {
  return requestJson<BenchmarkPack[]>("/benchmark-packs");
}

export interface AnalyticsFilters {
  benchmarkPackId?: UUID;
  repositoryId?: UUID;
  modelProvider?: string;
  modelName?: string;
  dateFrom?: string;
  dateTo?: string;
}

export interface ModelLeaderboardFilters
  extends Omit<AnalyticsFilters, "modelProvider" | "modelName"> {
  minRuns?: number;
}

export async function getAnalyticsSummary(
  filters: AnalyticsFilters = {},
): Promise<AnalyticsSummary> {
  return requestJson<AnalyticsSummary>(`/analytics/summary${analyticsQuery(filters)}`);
}

export async function getAnalyticsByRepository(
  filters: AnalyticsFilters = {},
): Promise<RepositoryAnalytics[]> {
  return requestJson<RepositoryAnalytics[]>(
    `/analytics/by-repository${analyticsQuery(filters)}`,
  );
}

export async function getAnalyticsByPack(
  filters: AnalyticsFilters = {},
): Promise<PackAnalytics[]> {
  return requestJson<PackAnalytics[]>(`/analytics/by-pack${analyticsQuery(filters)}`);
}

export async function getModelLeaderboard(
  filters: ModelLeaderboardFilters = {},
): Promise<ModelLeaderboardRow[]> {
  const params = analyticsParams(filters);
  if (filters.minRuns !== undefined) params.set("min_runs", String(filters.minRuns));
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<ModelLeaderboardRow[]>(`/analytics/model-leaderboard${query}`);
}

export async function getAgentRunDetails(runId: UUID): Promise<AgentRunDetail> {
  return requestJson<AgentRunDetail>(`/agent-runs/${runId}`);
}

export async function getRunPatch(runId: UUID): Promise<GeneratedPatch | null> {
  return requestNullable<GeneratedPatch>(`/agent-runs/${runId}/patch`);
}

export async function getRunTests(runId: UUID): Promise<TestResult[]> {
  return requestJson<TestResult[]>(`/agent-runs/${runId}/tests`);
}

export async function getRunMetrics(runId: UUID): Promise<EvaluationMetric | null> {
  return requestNullable<EvaluationMetric>(`/agent-runs/${runId}/metrics`);
}

export async function getPatchReview(patchId: UUID): Promise<PatchReview> {
  return requestJson<PatchReview>(`/patches/${patchId}/review`);
}

export async function approvePatch(
  patchId: UUID,
  request: PatchReviewRequest,
): Promise<PatchReview> {
  return requestJson<PatchReview>(`/patches/${patchId}/approve`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function rejectPatch(
  patchId: UUID,
  request: PatchReviewRequest,
): Promise<PatchReview> {
  return requestJson<PatchReview>(`/patches/${patchId}/reject`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

async function requestNullable<T>(path: string): Promise<T | null> {
  try {
    return await requestJson<T>(path);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

function analyticsQuery(filters: AnalyticsFilters): string {
  const params = analyticsParams(filters);
  return params.size ? `?${params.toString()}` : "";
}

function analyticsParams(filters: AnalyticsFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.benchmarkPackId) params.set("benchmark_pack_id", filters.benchmarkPackId);
  if (filters.repositoryId) params.set("repository_id", filters.repositoryId);
  if (filters.modelProvider) params.set("model_provider", filters.modelProvider);
  if (filters.modelName) params.set("model_name", filters.modelName);
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  return params;
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init.headers,
    },
  });

  if (!response.ok) {
    const payload = await parseError(response);
    throw new ApiError(errorMessage(payload, response.status), response.status, payload);
  }

  return (await response.json()) as T;
}

async function parseError(response: Response): Promise<ApiErrorPayload | null> {
  try {
    return (await response.json()) as ApiErrorPayload;
  } catch {
    return null;
  }
}

function errorMessage(payload: ApiErrorPayload | null, status: number): string {
  if (!payload || payload.detail === undefined) {
    return `Request failed with status ${status}.`;
  }
  if (typeof payload.detail === "string") {
    return payload.detail;
  }
  return JSON.stringify(payload.detail);
}
