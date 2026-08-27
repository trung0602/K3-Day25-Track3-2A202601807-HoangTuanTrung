from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery-time calculation:
    1. For each breaker in gateway.breakers.values():
       - Walk breaker.transition_log entries
       - Track when circuit goes to "open" (save ts)
       - Track when circuit goes to "closed" (compute delta from open ts)
       - Recovery time = (close_ts - open_ts) * 1000 (convert to ms)
    2. Return average of all recovery times, or None if no recovery occurred.

    Each transition_log entry is a dict with keys: "from", "to", "reason", "ts"
    where "ts" is time.time() (epoch seconds).
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            target = transition.get("to")
            timestamp = float(transition["ts"])
            if target == "open":
                opened_at = timestamp
            elif target == "closed" and opened_at is not None:
                recovery_times.append((timestamp - opened_at) * 1000.0)
                opened_at = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario.

    Scenario runner flow:
    1. Build gateway with build_gateway(config, scenario.provider_overrides or None)
    2. Create empty RunMetrics()
    3. Loop config.load_test.requests times:
       a. Pick random query from queries
       b. Call gateway.complete(prompt)
       c. Update metrics:
          - total_requests += 1
          - estimated_cost += result.estimated_cost
          - If cache_hit: cache_hits += 1, estimated_cost_saved += 0.001
          - If route == "fallback": fallback_successes += 1, successful_requests += 1
          - If route == "static_fallback": static_fallbacks += 1, failed_requests += 1
          - Else: successful_requests += 1
          - If result.latency_ms > 0: append to latencies_ms
    4. Count circuit_open_count from breaker transition logs (entries where to == "open")
    5. Set recovery_time_ms via calculate_recovery_time_ms(gateway)
    6. Return metrics
    """
    if not queries:
        raise ValueError("at least one query is required to run a scenario")

    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    try:
        # Each scenario starts from a clean shared-cache namespace, so results
        # do not depend on which scenario happened to run before it.
        if isinstance(gateway.cache, SharedRedisCache):
            gateway.cache.flush()

        for _ in range(config.load_test.requests):
            result = gateway.complete(random.choice(queries))
            metrics.total_requests += 1
            metrics.estimated_cost += result.estimated_cost

            if result.cache_hit:
                metrics.cache_hits += 1
                metrics.estimated_cost_saved += 0.001

            if result.route == "fallback":
                metrics.fallback_successes += 1
                metrics.successful_requests += 1
            elif result.route == "static_fallback":
                metrics.static_fallbacks += 1
                metrics.failed_requests += 1
            else:
                metrics.successful_requests += 1

            if result.latency_ms > 0:
                metrics.latencies_ms.append(result.latency_ms)

        metrics.circuit_open_count = sum(
            1
            for breaker in gateway.breakers.values()
            for transition in breaker.transition_log
            if transition.get("to") == "open"
        )
        metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
        return metrics
    finally:
        if isinstance(gateway.cache, SharedRedisCache):
            gateway.cache.close()


def _scenario_passed(
    scenario: ScenarioConfig, metrics: RunMetrics, provider_names: list[str]
) -> bool:
    """Evaluate the behavior promised by a chaos scenario, not just any success."""
    all_forced_down = bool(provider_names) and all(
        scenario.provider_overrides.get(name, -1.0) >= 1.0 for name in provider_names
    )

    if all_forced_down:
        return metrics.static_fallbacks > 0 and metrics.circuit_open_count > 0
    if scenario.provider_overrides.get("primary") == 1.0:
        return (
            metrics.availability >= 0.95
            and metrics.fallback_successes > 0
            and metrics.circuit_open_count > 0
        )
    if not scenario.provider_overrides:
        return metrics.availability >= 0.99 and metrics.static_fallbacks == 0
    return metrics.availability >= 0.95


def _comparison_summary(metrics: RunMetrics) -> dict[str, object]:
    """Return the fields needed to compare cache-enabled and cache-disabled runs."""
    return {
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "estimated_cost_saved": round(metrics.estimated_cost_saved, 6),
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    Includes a controlled cache vs no-cache comparison after the named scenarios.
    """
    random.seed(2026)

    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    scenario_recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = _scenario_passed(scenario, result, [provider.name for provider in config.providers])
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details[scenario.name] = result.to_report_dict()

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            scenario_recovery_times.append(result.recovery_time_ms)

    if scenario_recovery_times:
        combined.recovery_time_ms = sum(scenario_recovery_times) / len(scenario_recovery_times)

    # Use an all-healthy provider set so the comparison isolates cache effects
    # rather than random provider failures.  Reset the seed before both runs so
    # they receive the same query order and simulated provider jitter.
    healthy = ScenarioConfig(
        name="cache_comparison",
        description="All providers healthy; compare cache enabled and disabled",
        provider_overrides={provider.name: 0.0 for provider in config.providers},
    )
    without_cache_config = config.model_copy(deep=True)
    without_cache_config.cache.enabled = False
    with_cache_config = config.model_copy(deep=True)
    with_cache_config.cache.enabled = True

    random.seed(2026)
    without_cache = run_scenario(without_cache_config, queries, healthy)
    random.seed(2026)
    with_cache = run_scenario(with_cache_config, queries, healthy)
    combined.cache_comparison = {
        "without_cache": _comparison_summary(without_cache),
        "with_cache": _comparison_summary(with_cache),
    }

    return combined
