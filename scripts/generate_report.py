from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.config import load_config


def _format(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _met(actual: float | None, target: float, *, minimum: bool) -> str:
    if actual is None:
        return "N/A"
    passed = actual >= target if minimum else actual < target
    return "Đạt" if passed else "Chưa đạt"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    recovery = metrics.get("recovery_time_ms")
    recovery_number = float(recovery) if recovery is not None else None

    lines = [
        "# Báo cáo Day 25 — Reliability Engineering for Production Agents",
        "",
        "**Họ và tên:** Hoàng Tuấn Trung  ",
        "**Mã sinh viên:** 2A202601807",
        "",
        "## 1. Mục tiêu và nội dung",
        "",
        (
            "Mục tiêu của lab là xây một lớp reliability cho LLM gateway: lỗi provider không làm "
            "toàn hệ thống ngừng phục vụ, truy vấn lặp lại được trả nhanh và rẻ hơn, dữ liệu nhạy "
            "cảm không đi vào cache, và hành vi khi có lỗi được đo bằng số liệu tái lập."
        ),
        "",
        (
            "Các nội dung đã hoàn thành: circuit breaker CLOSED/OPEN/HALF_OPEN; semantic cache "
            "n-gram cosine và guardrail; Redis shared cache; chuỗi primary/fallback/static; chaos "
            "scenario; P50/P95/P99, availability, error/cache/fallback rate, recovery time và cost."
        ),
        "",
        "## 2. Kiến trúc và luồng xử lý",
        "",
        "```text",
        "Client",
        "  |",
        "  v",
        "ReliabilityGateway --> Semantic Cache -- HIT --> trả ngay (cost = 0)",
        "  | MISS / privacy bypass",
        "  v",
        "Circuit Breaker(primary) -- allowed --> Primary Provider",
        "  | OPEN / provider error                 | success --> ghi cache --> trả kết quả",
        "  v",
        "Circuit Breaker(backup)  -- allowed --> Backup Provider",
        "  | OPEN / provider error                 | success --> ghi cache --> trả fallback",
        "  v",
        "Static fallback (degraded response, không phát sinh chi phí)",
        "```",
        "",
        (
            "Circuit breaker mở sau số lỗi liên tiếp đạt ngưỡng, fail-fast trong thời gian reset, "
            "sau đó cho probe ở HALF_OPEN. Probe thành công đóng mạch; probe lỗi mở lại ngay."
        ),
        "",
        "## 3. Cấu hình và lý do",
        "",
        "| Thiết lập | Giá trị | Lý do |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Cô lập provider sau 3 lỗi liên tiếp nhưng không quá nhạy với một lỗi đơn lẻ. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Fail-fast ngắn hạn rồi thăm dò khả năng hồi phục. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | Một probe thành công đủ đóng mạch trong mô phỏng đơn luồng. |",
        f"| cache backend | {config.cache.backend} | Memory mặc định để lab tự chạy; Redis dùng cho nhiều instance. |",
        f"| cache TTL (giây) | {config.cache.ttl_seconds} | Hạn chế dữ liệu cũ và kích thước cache. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | Ngưỡng cao 0.92 giảm semantic false hit. |",
        f"| requests/scenario | {config.load_test.requests} | Đủ mẫu để tính percentile và tỷ lệ. |",
        "",
        "## 4. SLO và kết quả tổng hợp",
        "",
        "| SLI | SLO | Thực tế | Kết quả |",
        "|---|---:|---:|---|",
        f"| Availability | >= 99% | {_format(metrics.get('availability'))} | {_met(float(metrics['availability']), 0.99, minimum=True)} |",
        f"| P95 latency | < 2500 ms | {_format(metrics.get('latency_p95_ms'))} ms | {_met(float(metrics['latency_p95_ms']), 2500, minimum=False)} |",
        f"| Fallback success rate | >= 95% | {_format(metrics.get('fallback_success_rate'))} | {_met(float(metrics['fallback_success_rate']), 0.95, minimum=True)} |",
        f"| Cache hit rate | >= 10% | {_format(metrics.get('cache_hit_rate'))} | {_met(float(metrics['cache_hit_rate']), 0.10, minimum=True)} |",
        f"| Recovery time | < 5000 ms | {_format(recovery)} | {_met(recovery_number, 5000, minimum=False)} |",
        "",
        (
            "Availability tổng hợp có cả kịch bản chủ đích làm sập toàn bộ provider; vì vậy có "
            "thể thấp hơn SLO production dù static fallback hoạt động đúng như thiết kế chaos."
        ),
        "",
        "## 5. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in (
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    ):
        lines.append(f"| {key} | {_format(metrics.get(key))} |")

    lines += [
        "",
        "## 6. Chaos scenarios",
        "",
        "| Scenario | Kỳ vọng | Quan sát | Kết quả |",
        "|---|---|---|---|",
    ]
    expected = {
        "primary_timeout_100": "Primary mở mạch, backup tiếp quản.",
        "primary_flaky_50": "Lỗi primary được hấp thụ qua fallback/cache.",
        "all_healthy": "Không static fallback, availability >= 99%.",
        "all_providers_down": "Hai mạch mở và gateway degrade có kiểm soát.",
    }
    details = metrics.get("scenario_details", {})
    for name, status in metrics.get("scenarios", {}).items():
        item = details.get(name, {})
        observed = (
            f"availability={_format(item.get('availability'))}, "
            f"cache_hit_rate={_format(item.get('cache_hit_rate'))}, "
            f"circuit_open_count={_format(item.get('circuit_open_count'))}"
        )
        lines.append(
            f"| {name} | {expected.get(name, 'Gateway phục vụ hoặc degrade an toàn.')} "
            f"| {observed} | {str(status).upper()} |"
        )

    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    lines += [
        "",
        "## 7. So sánh cache",
        "",
        "Hai lượt dùng cùng seed, cùng thứ tự query và provider khỏe để cô lập tác động của cache.",
        "",
        "| Metric | Không cache | Có cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"):
        before = float(without_cache.get(key, 0))
        after = float(with_cache.get(key, 0))
        lines.append(f"| {key} | {before:.4f} | {after:.4f} | {after - before:+.4f} |")

    lines += [
        "",
        (
            "Percentile chỉ tính lượt thực sự gọi provider theo đặc tả lab; cache hit có latency 0 "
            "và không được đưa vào percentile. Chi phí và hit rate phản ánh tác động cache rõ hơn."
        ),
        "",
        "## 8. Redis shared cache",
        "",
        (
            "Cache RAM bị tách theo process và mất khi restart. `SharedRedisCache` lưu Redis Hash "
            "theo hash query, đặt EXPIRE, hỗ trợ exact lookup và similarity scan nên nhiều gateway "
            "instance đọc chung dữ liệu. Hai backend dùng chung privacy và false-hit guardrail."
        ),
        "",
        (
            "Integration test `test_shared_state_across_instances` tạo hai client cùng prefix, "
            "ghi ở instance 1 và đọc ở instance 2. Khi Redis local chưa hoạt động, pytest skip 6 "
            "test này thay vì tạo bằng chứng giả. Cách xác minh:"
        ),
        "",
        "```bash",
        "docker compose up -d",
        "pytest tests/test_redis_cache.py -v",
        'docker compose exec redis redis-cli KEYS "rl:cache:*"',
        "```",
        "",
        "## 9. Phân tích điểm yếu",
        "",
        (
            "HALF_OPEN hiện phù hợp mô phỏng tuần tự nhưng chưa giới hạn đúng một probe khi nhiều "
            "thread/process truy cập. Similarity scan Redis cũng là O(N). Trước production nên "
            "dùng distributed lock/token cho probe, chia sẻ breaker state, và thay scan bằng "
            "vector index."
        ),
        "",
        "## 10. Các bước tái lập",
        "",
        "```bash",
        'python -m pip install -e ".[dev]"',
        "docker compose up -d       # nếu Docker khả dụng",
        "pytest -q",
        "ruff check src tests scripts",
        "mypy src",
        "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json",
        "python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md",
        "```",
        "",
        "## 11. Hướng phát triển",
        "",
        "1. Giới hạn một HALF_OPEN probe và chia sẻ circuit state qua Redis.",
        "2. Thêm concurrent load test, rate limit theo tenant và budget-aware routing.",
        "3. Thêm quality SLO/evaluation cho cache hit đúng ngữ nghĩa.",
    ]

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
