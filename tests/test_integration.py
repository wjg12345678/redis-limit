import concurrent.futures
import threading
import time

from fastapi.testclient import TestClient

import redis_limiter
from examples.fastapi_demo import create_app


def build_redis_config(host: str = "redis") -> redis_limiter.RedisConfig:
    config = redis_limiter.RedisConfig()
    config.host = host
    config.port = 6379
    config.pool_size = 4
    config.connect_timeout_ms = 200
    config.socket_timeout_ms = 200
    return config


def test_token_bucket_denies_after_capacity() -> None:
    pool = redis_limiter.RedisPool(build_redis_config())
    limiter = redis_limiter.TokenBucketLimiter(
        pool,
        max_tokens=3,
        refill_rate=0.01,
        key_prefix=f"pytest:token:{int(time.time() * 1000)}:",
    )

    key = "user:42"
    results = [limiter.allow(key) for _ in range(4)]

    assert [result.allowed for result in results] == [True, True, True, False]
    assert results[3].retry_after_ms > 0
    assert all(result.backend_status == redis_limiter.BackendStatus.Healthy for result in results)


def test_sliding_window_shared_key_does_not_over_issue_under_concurrency() -> None:
    pool = redis_limiter.RedisPool(build_redis_config())
    config = redis_limiter.RateLimitConfig()
    config.max_requests = 20
    config.window_size_ms = 1000
    config.key_prefix = f"pytest:sliding:{int(time.time() * 1000)}:"
    limiter = redis_limiter.SlidingWindowLimiter(pool, config)

    worker_count = 8
    requests_per_worker = 10
    start_barrier = threading.Barrier(worker_count)

    def run_worker() -> list[redis_limiter.RateLimitResult]:
        start_barrier.wait()
        return [limiter.allow("hot:key") for _ in range(requests_per_worker)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(run_worker) for _ in range(worker_count)]

    results = [result for future in futures for result in future.result()]
    allowed_count = sum(1 for result in results if result.allowed)
    denied_count = sum(1 for result in results if not result.allowed)

    assert len(results) == worker_count * requests_per_worker
    assert allowed_count == config.max_requests
    assert denied_count == len(results) - config.max_requests
    assert all(result.backend_status == redis_limiter.BackendStatus.Healthy for result in results)


def test_resilient_limiter_falls_back_when_redis_unavailable() -> None:
    pool = redis_limiter.RedisPool(build_redis_config("redis-unavailable"))
    remote = redis_limiter.TokenBucketLimiter(
        pool,
        max_tokens=3,
        refill_rate=1.0,
        key_prefix=f"pytest:fallback:{int(time.time() * 1000)}:",
    )
    limiter = redis_limiter.ResilientTokenBucketLimiter(
        remote,
        redis_limiter.FallbackMode.LocalTokenBucket,
        local_max_tokens=2,
        local_refill_rate=0.01,
    )

    results = [limiter.allow("sms:user:7") for _ in range(3)]

    assert [result.allowed for result in results] == [True, True, False]
    assert limiter.redis_error_count() == 3
    assert limiter.fallback_hit_count() == 3
    assert all(result.backend_status == redis_limiter.BackendStatus.Fallback for result in results)


def test_fastapi_demo_limits_requests() -> None:
    app = create_app(
        redis_host="redis",
        max_tokens=2,
        refill_rate=0.01,
        local_max_tokens=2,
        local_refill_rate=0.01,
        key_prefix=f"pytest:api:{int(time.time() * 1000)}:",
    )
    client = TestClient(app)

    health_response = client.get("/healthz")
    assert health_response.status_code == 200
    assert health_response.json()["redis_healthy"] is True

    responses = [
        client.post("/rate-limit/check", json={"key": "login:user:1"})
        for _ in range(3)
    ]
    payloads = [response.json() for response in responses]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [payload["allowed"] for payload in payloads] == [True, True, False]
    assert payloads[2]["retry_after_ms"] > 0
    assert all(payload["backend_status"] == "Healthy" for payload in payloads)


def test_fastapi_demo_exposes_fallback_state() -> None:
    app = create_app(
        redis_host="redis-unavailable",
        max_tokens=2,
        refill_rate=0.01,
        local_max_tokens=1,
        local_refill_rate=0.01,
        key_prefix=f"pytest:api:fallback:{int(time.time() * 1000)}:",
    )
    client = TestClient(app)

    health_response = client.get("/healthz")
    assert health_response.status_code == 200
    assert health_response.json()["redis_healthy"] is False

    first = client.post("/rate-limit/check", json={"key": "sms:user:9"}).json()
    second = client.post("/rate-limit/check", json={"key": "sms:user:9"}).json()

    assert first["allowed"] is True
    assert second["allowed"] is False
    assert first["backend_status"] == "Fallback"
    assert second["backend_status"] == "Fallback"
    assert second["fallback_hit_count"] >= 2
    assert second["redis_error_count"] >= 2


def test_fastapi_demo_exposes_metrics() -> None:
    app = create_app(
        redis_host="redis",
        max_tokens=2,
        refill_rate=0.01,
        local_max_tokens=2,
        local_refill_rate=0.01,
        key_prefix=f"pytest:api:metrics:{int(time.time() * 1000)}:",
    )
    client = TestClient(app)

    client.post("/rate-limit/check", json={"key": "metrics:user:1"})
    client.post("/rate-limit/check", json={"key": "metrics:user:1"})
    client.post("/rate-limit/check", json={"key": "metrics:user:1"})

    response = client.get("/metrics")
    body = response.text

    assert response.status_code == 200
    assert "demo_rate_limit_requests_total 3" in body
    assert "demo_rate_limit_allowed_total 2" in body
    assert "demo_rate_limit_denied_total 1" in body
    assert "demo_redis_health 1" in body
