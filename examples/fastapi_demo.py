import os
import threading
import time
from typing import Literal

import redis_limiter
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


def build_redis_config(host: str | None = None, port: int | None = None) -> redis_limiter.RedisConfig:
    config = redis_limiter.RedisConfig()
    config.host = host or os.getenv("REDIS_HOST", "127.0.0.1")
    config.port = port or int(os.getenv("REDIS_PORT", "6379"))
    config.pool_size = int(os.getenv("REDIS_POOL_SIZE", "8"))
    config.connect_timeout_ms = int(os.getenv("REDIS_CONNECT_TIMEOUT_MS", "200"))
    config.socket_timeout_ms = int(os.getenv("REDIS_SOCKET_TIMEOUT_MS", "200"))
    return config


def parse_fallback_mode(mode: str) -> redis_limiter.FallbackMode:
    normalized = mode.strip().lower()
    if normalized == "failopen":
        return redis_limiter.FallbackMode.FailOpen
    if normalized == "failclosed":
        return redis_limiter.FallbackMode.FailClosed
    return redis_limiter.FallbackMode.LocalTokenBucket


def fallback_mode_name(mode: redis_limiter.FallbackMode) -> str:
    if mode == redis_limiter.FallbackMode.FailOpen:
        return "FailOpen"
    if mode == redis_limiter.FallbackMode.FailClosed:
        return "FailClosed"
    return "LocalTokenBucket"


class RateLimitRequest(BaseModel):
    key: str = Field(..., min_length=1)
    tokens_needed: int = Field(default=1, ge=1)


class RateLimitResponse(BaseModel):
    allowed: bool
    current_count: int
    remaining: int
    reset_after_ms: int
    retry_after_ms: int
    backend_status: Literal["Healthy", "Unavailable", "Fallback"]
    fallback_mode: Literal["FailOpen", "FailClosed", "LocalTokenBucket"]
    redis_error_count: int
    fallback_hit_count: int


def backend_status_name(status: redis_limiter.BackendStatus) -> str:
    if status == redis_limiter.BackendStatus.Unavailable:
        return "Unavailable"
    if status == redis_limiter.BackendStatus.Fallback:
        return "Fallback"
    return "Healthy"


class DemoMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests_total = 0
        self.allowed_total = 0
        self.denied_total = 0
        self.redis_error_total = 0
        self.fallback_total = 0
        self.request_duration_seconds_sum = 0.0

    def observe(
        self,
        *,
        allowed: bool,
        redis_error_delta: int,
        fallback_delta: int,
        duration_seconds: float,
    ) -> None:
        with self._lock:
            self.requests_total += 1
            if allowed:
                self.allowed_total += 1
            else:
                self.denied_total += 1
            self.redis_error_total += max(0, redis_error_delta)
            self.fallback_total += max(0, fallback_delta)
            self.request_duration_seconds_sum += duration_seconds

    def render_prometheus(self, *, redis_healthy: bool, fallback_mode: str) -> str:
        fallback_value = {
            "FailOpen": 0,
            "FailClosed": 1,
            "LocalTokenBucket": 2,
        }[fallback_mode]
        with self._lock:
            lines = [
                "# HELP demo_rate_limit_requests_total Total number of rate-limit API requests.",
                "# TYPE demo_rate_limit_requests_total counter",
                f"demo_rate_limit_requests_total {self.requests_total}",
                "# HELP demo_rate_limit_allowed_total Total number of allowed rate-limit API requests.",
                "# TYPE demo_rate_limit_allowed_total counter",
                f"demo_rate_limit_allowed_total {self.allowed_total}",
                "# HELP demo_rate_limit_denied_total Total number of denied rate-limit API requests.",
                "# TYPE demo_rate_limit_denied_total counter",
                f"demo_rate_limit_denied_total {self.denied_total}",
                "# HELP demo_rate_limit_redis_error_total Total number of Redis errors observed by the API.",
                "# TYPE demo_rate_limit_redis_error_total counter",
                f"demo_rate_limit_redis_error_total {self.redis_error_total}",
                "# HELP demo_rate_limit_fallback_total Total number of fallback executions observed by the API.",
                "# TYPE demo_rate_limit_fallback_total counter",
                f"demo_rate_limit_fallback_total {self.fallback_total}",
                "# HELP demo_rate_limit_request_duration_seconds_sum Sum of request durations for the rate-limit API.",
                "# TYPE demo_rate_limit_request_duration_seconds_sum counter",
                f"demo_rate_limit_request_duration_seconds_sum {self.request_duration_seconds_sum:.6f}",
                "# HELP demo_redis_health Redis health status reported by the demo app. 1 means healthy.",
                "# TYPE demo_redis_health gauge",
                f"demo_redis_health {1 if redis_healthy else 0}",
                "# HELP demo_fallback_mode Current fallback mode. FailOpen=0, FailClosed=1, LocalTokenBucket=2.",
                "# TYPE demo_fallback_mode gauge",
                f"demo_fallback_mode {fallback_value}",
            ]
        return "\n".join(lines) + "\n"


def create_app(
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    max_tokens: int | None = None,
    refill_rate: float | None = None,
    local_max_tokens: int | None = None,
    local_refill_rate: float | None = None,
    key_prefix: str | None = None,
    fallback_mode: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Redis Rate Limiter Demo", version="1.0.0")

    redis_config = build_redis_config(redis_host, redis_port)
    pool = redis_limiter.RedisPool(redis_config)
    remote = redis_limiter.TokenBucketLimiter(
        pool,
        max_tokens=max_tokens or int(os.getenv("RATE_LIMIT_MAX_TOKENS", "20")),
        refill_rate=refill_rate or float(os.getenv("RATE_LIMIT_REFILL_RATE", "5")),
        key_prefix=key_prefix or os.getenv("RATE_LIMIT_KEY_PREFIX", "api:tokenbucket:"),
    )
    limiter = redis_limiter.ResilientTokenBucketLimiter(
        remote,
        parse_fallback_mode(fallback_mode or os.getenv("RATE_LIMIT_FALLBACK_MODE", "LocalTokenBucket")),
        local_max_tokens=local_max_tokens or int(os.getenv("LOCAL_MAX_TOKENS", "10")),
        local_refill_rate=local_refill_rate or float(os.getenv("LOCAL_REFILL_RATE", "2")),
    )

    metrics = DemoMetrics()
    app.state.pool = pool
    app.state.limiter = limiter
    app.state.metrics = metrics

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        redis_healthy = pool.health_check()
        return {
            "ok": True,
            "redis_healthy": redis_healthy,
            "fallback_mode": fallback_mode_name(limiter.fallback_mode()),
        }

    @app.post("/rate-limit/check", response_model=RateLimitResponse)
    def check_rate_limit(request: RateLimitRequest) -> RateLimitResponse:
        before_redis_errors = limiter.redis_error_count()
        before_fallback_hits = limiter.fallback_hit_count()
        started_at = time.perf_counter()
        result = limiter.allow(request.key, request.tokens_needed)
        after_redis_errors = limiter.redis_error_count()
        after_fallback_hits = limiter.fallback_hit_count()
        metrics.observe(
            allowed=result.allowed,
            redis_error_delta=after_redis_errors - before_redis_errors,
            fallback_delta=after_fallback_hits - before_fallback_hits,
            duration_seconds=time.perf_counter() - started_at,
        )
        return RateLimitResponse(
            allowed=result.allowed,
            current_count=result.current_count,
            remaining=result.remaining,
            reset_after_ms=result.reset_after_ms,
            retry_after_ms=result.retry_after_ms,
            backend_status=backend_status_name(result.backend_status),
            fallback_mode=fallback_mode_name(limiter.fallback_mode()),
            redis_error_count=after_redis_errors,
            fallback_hit_count=after_fallback_hits,
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint() -> PlainTextResponse:
        redis_healthy = pool.health_check()
        fallback_mode = fallback_mode_name(limiter.fallback_mode())
        return PlainTextResponse(
            metrics.render_prometheus(redis_healthy=redis_healthy, fallback_mode=fallback_mode),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


app = create_app()
