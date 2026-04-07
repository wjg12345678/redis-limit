import os
import time

import redis_limiter


def main() -> None:
    redis_config = redis_limiter.RedisConfig()
    redis_config.host = os.getenv("REDIS_HOST", "127.0.0.1")
    redis_config.port = int(os.getenv("REDIS_PORT", "6379"))
    redis_config.pool_size = 4

    pool = redis_limiter.RedisPool(redis_config)
    limiter = redis_limiter.TokenBucketLimiter(pool, max_tokens=5, refill_rate=2.0)

    user_key = "api:user:42"
    for idx in range(8):
        result = limiter.allow(user_key)
        print(
            f"request={idx} allowed={result.allowed} "
            f"remaining={result.remaining} retry_after_ms={result.retry_after_ms}"
        )
        time.sleep(0.2)


if __name__ == "__main__":
    main()
