# Báo cáo Day 25 — Reliability Engineering for Production Agents

**Họ và tên:** Hoàng Tuấn Trung  
**Mã sinh viên:** 2A202601807

## 1. Mục tiêu và nội dung

Mục tiêu của lab là xây một lớp reliability cho LLM gateway: lỗi provider không làm toàn hệ thống ngừng phục vụ, truy vấn lặp lại được trả nhanh và rẻ hơn, dữ liệu nhạy cảm không đi vào cache, và hành vi khi có lỗi được đo bằng số liệu tái lập.

Các nội dung đã hoàn thành: circuit breaker CLOSED/OPEN/HALF_OPEN; semantic cache n-gram cosine và guardrail; Redis shared cache; chuỗi primary/fallback/static; chaos scenario; P50/P95/P99, availability, error/cache/fallback rate, recovery time và cost.

## 2. Kiến trúc và luồng xử lý

```text
Client
  |
  v
ReliabilityGateway --> Semantic Cache -- HIT --> trả ngay (cost = 0)
  | MISS / privacy bypass
  v
Circuit Breaker(primary) -- allowed --> Primary Provider
  | OPEN / provider error                 | success --> ghi cache --> trả kết quả
  v
Circuit Breaker(backup)  -- allowed --> Backup Provider
  | OPEN / provider error                 | success --> ghi cache --> trả fallback
  v
Static fallback (degraded response, không phát sinh chi phí)
```

Circuit breaker mở sau số lỗi liên tiếp đạt ngưỡng, fail-fast trong thời gian reset, sau đó cho probe ở HALF_OPEN. Probe thành công đóng mạch; probe lỗi mở lại ngay.

## 3. Cấu hình và lý do

| Thiết lập | Giá trị | Lý do |
|---|---:|---|
| failure_threshold | 3 | Cô lập provider sau 3 lỗi liên tiếp nhưng không quá nhạy với một lỗi đơn lẻ. |
| reset_timeout_seconds | 2.0 | Fail-fast ngắn hạn rồi thăm dò khả năng hồi phục. |
| success_threshold | 1 | Một probe thành công đủ đóng mạch trong mô phỏng đơn luồng. |
| cache backend | memory | Memory mặc định để lab tự chạy; Redis dùng cho nhiều instance. |
| cache TTL (giây) | 300 | Hạn chế dữ liệu cũ và kích thước cache. |
| similarity_threshold | 0.92 | Ngưỡng cao 0.92 giảm semantic false hit. |
| requests/scenario | 100 | Đủ mẫu để tính percentile và tỷ lệ. |

## 4. SLO và kết quả tổng hợp

| SLI | SLO | Thực tế | Kết quả |
|---|---:|---:|---|
| Availability | >= 99% | 0.7375 | Chưa đạt |
| P95 latency | < 2500 ms | 314.4900 ms | Đạt |
| Fallback success rate | >= 95% | 0.4000 | Chưa đạt |
| Cache hit rate | >= 10% | 0.4500 | Đạt |
| Recovery time | < 5000 ms | 2223.0580 | Đạt |

Availability tổng hợp có cả kịch bản chủ đích làm sập toàn bộ provider; vì vậy có thể thấp hơn SLO production dù static fallback hoạt động đúng như thiết kế chaos.

## 5. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.7375 |
| error_rate | 0.2625 |
| latency_p50_ms | 269.6300 |
| latency_p95_ms | 314.4900 |
| latency_p99_ms | 319.4200 |
| fallback_success_rate | 0.4000 |
| cache_hit_rate | 0.4500 |
| circuit_open_count | 11 |
| recovery_time_ms | 2223.0580 |
| estimated_cost | 0.0495 |
| estimated_cost_saved | 0.1800 |

## 6. Chaos scenarios

| Scenario | Kỳ vọng | Quan sát | Kết quả |
|---|---|---|---|
| primary_timeout_100 | Primary mở mạch, backup tiếp quản. | availability=0.9700, cache_hit_rate=0.6600, circuit_open_count=5 | PASS |
| primary_flaky_50 | Lỗi primary được hấp thụ qua fallback/cache. | availability=0.9800, cache_hit_rate=0.5700, circuit_open_count=4 | PASS |
| all_healthy | Không static fallback, availability >= 99%. | availability=1.0000, cache_hit_rate=0.5700, circuit_open_count=0 | PASS |
| all_providers_down | Hai mạch mở và gateway degrade có kiểm soát. | availability=0.0000, cache_hit_rate=0.0000, circuit_open_count=2 | PASS |

## 7. So sánh cache

Hai lượt dùng cùng seed, cùng thứ tự query và provider khỏe để cô lập tác động của cache.

| Metric | Không cache | Có cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 212.9900 | 215.5200 | +2.5300 |
| latency_p95_ms | 237.3700 | 238.5100 | +1.1400 |
| estimated_cost | 0.0603 | 0.0222 | -0.0381 |
| cache_hit_rate | 0.0000 | 0.6400 | +0.6400 |

Percentile chỉ tính lượt thực sự gọi provider theo đặc tả lab; cache hit có latency 0 và không được đưa vào percentile. Chi phí và hit rate phản ánh tác động cache rõ hơn.

## 8. Redis shared cache

Cache RAM bị tách theo process và mất khi restart. `SharedRedisCache` lưu Redis Hash theo hash query, đặt EXPIRE, hỗ trợ exact lookup và similarity scan nên nhiều gateway instance đọc chung dữ liệu. Hai backend dùng chung privacy và false-hit guardrail.

Integration test `test_shared_state_across_instances` tạo hai client cùng prefix, ghi ở instance 1 và đọc ở instance 2. Khi Redis local chưa hoạt động, pytest skip 6 test này thay vì tạo bằng chứng giả. Cách xác minh:

```bash
docker compose up -d
pytest tests/test_redis_cache.py -v
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

## 9. Phân tích điểm yếu

HALF_OPEN hiện phù hợp mô phỏng tuần tự nhưng chưa giới hạn đúng một probe khi nhiều thread/process truy cập. Similarity scan Redis cũng là O(N). Trước production nên dùng distributed lock/token cho probe, chia sẻ breaker state, và thay scan bằng vector index.

## 10. Các bước tái lập

```bash
python -m pip install -e ".[dev]"
docker compose up -d       # nếu Docker khả dụng
pytest -q
ruff check src tests scripts
mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md
```

## 11. Hướng phát triển

1. Giới hạn một HALF_OPEN probe và chia sẻ circuit state qua Redis.
2. Thêm concurrent load test, rate limit theo tenant và budget-aware routing.
3. Thêm quality SLO/evaluation cho cache hit đúng ngữ nghĩa.
