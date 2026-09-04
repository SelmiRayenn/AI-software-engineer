import { apiBaseUrl } from "./config";
import type {
  AgentRun,
  AgentRunDetail,
  ApiErrorPayload,
  BenchmarkTask,
  EvaluationMetric,
  GeneratedPatch,
  PatchReview,
  PatchReviewRequest,
  Repository,
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

export async function listBenchmarkTasks(): Promise<BenchmarkTask[]> {
  return requestJson<BenchmarkTask[]>("/api/v1/benchmark-tasks");
}

export async function listAgentRuns(): Promise<AgentRun[]> {
  return requestJson<AgentRun[]>("/api/v1/agent-runs");
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
