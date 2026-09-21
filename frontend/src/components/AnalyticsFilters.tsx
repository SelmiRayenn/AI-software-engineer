import type { BenchmarkPack, Repository } from "../types/api";

export interface AnalyticsFilterValue {
  modelProvider: string;
  repositoryId: string;
  benchmarkPackId: string;
}

interface AnalyticsFiltersProps {
  value: AnalyticsFilterValue;
  providers: string[];
  repositories: Repository[];
  packs: BenchmarkPack[];
  onChange: (value: AnalyticsFilterValue) => void;
}

export const emptyAnalyticsFilters: AnalyticsFilterValue = {
  modelProvider: "",
  repositoryId: "",
  benchmarkPackId: "",
};

export function AnalyticsFilters({
  value,
  providers,
  repositories,
  packs,
  onChange,
}: AnalyticsFiltersProps) {
  function update(field: keyof AnalyticsFilterValue, nextValue: string) {
    onChange({ ...value, [field]: nextValue });
  }

  const hasFilters = Boolean(value.modelProvider || value.repositoryId || value.benchmarkPackId);

  return (
    <section className="analytics-filters" aria-label="Analytics filters">
      <label>
        <span>Provider</span>
        <select
          value={value.modelProvider}
          onChange={(event) => update("modelProvider", event.target.value)}
        >
          <option value="">All providers</option>
          {providers.map((provider) => (
            <option key={provider} value={provider}>
              {provider}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Repository</span>
        <select
          value={value.repositoryId}
          onChange={(event) => update("repositoryId", event.target.value)}
        >
          <option value="">All repositories</option>
          {repositories.map((repository) => (
            <option key={repository.id} value={repository.id}>
              {repository.owner}/{repository.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Benchmark pack</span>
        <select
          value={value.benchmarkPackId}
          onChange={(event) => update("benchmarkPackId", event.target.value)}
        >
          <option value="">All packs</option>
          {packs.map((pack) => (
            <option key={pack.id} value={pack.id}>
              {pack.name} ({pack.version})
            </option>
          ))}
        </select>
      </label>
      <button
        className="secondary-button filter-reset"
        disabled={!hasFilters}
        onClick={() => onChange(emptyAnalyticsFilters)}
        type="button"
      >
        Clear filters
      </button>
    </section>
  );
}
