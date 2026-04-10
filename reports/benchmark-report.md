# 压测报告

Redis 分布式限流组件

日期：`2026-04-10`

## 一页结论

- 在当前 Docker 测试环境下，Redis 令牌桶限流的吞吐约为 `12.6k` 到 `13.0k QPS`
- 在热点 key 严格有效性压测下，理论最大放行 `45` 次，实际放行 `45` 次
- `over_issued=0.00`，本次压测没有观察到超发
- 功能验证、FastAPI 接入、指标导出、`pytest` 回归测试均通过

## 压测范围

- 吞吐压测
  - `4` 个 worker
  - `5s` 持续时间
  - 独立 key 和热点 key 两种场景
- 限流有效性压测
  - `4` 个 worker
  - `5s` 持续时间
  - 热点 key
  - `max_tokens=20`
  - `refill_rate=5`
  - 严格断言：`max-over-issue=0`、`max-over-issue-ratio=0`

## 测试环境

| 项目 | 值 |
| --- | --- |
| 运行方式 | Docker Compose |
| Redis | `redis:7-alpine` |
| 应用栈 | `C++17 + hiredis + pybind11 + FastAPI` |
| 限流路径 | Redis Lua Token Bucket |
| 验证链路 | `test`、`pytest`、`bench`、`/metrics` |

## 吞吐结果

| 场景 | Workers | 时长 | 总请求数 | QPS | 平均延迟(us) | P95(us) | P99(us) | 错误数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 独立 key | 4 | 5s | 62945 | 12589.00 | 316.24 | 571.30 | 823.68 | 0 |
| 热点 key | 4 | 5s | 64957 | 12991.40 | 304.11 | 447.45 | 751.25 | 0 |

![吞吐 QPS 对比](../assets/charts/throughput-qps.svg)

![吞吐 P95 延迟对比](../assets/charts/throughput-p95.svg)

结果解读：

- 两组场景都稳定在 `12k+ QPS`
- 这次压测里，热点 key 没有明显低于独立 key
- 未观察到执行错误

## 限流有效性结果

| 场景 | Workers | 时长 | Max Tokens | Refill Rate | 理论放行 | 实际放行 | 超发量 | 超发比例 | 拒绝率 | 断言结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 热点 key | 4 | 5s | 20 | 5/s | 45.00 | 45 | 0.00 | 0.000000 | 0.9991 | PASS |

![限流有效性柱状图](../assets/charts/effectiveness-bars.svg)

![允许/拒绝占比图](../assets/charts/effectiveness-pie.svg)

结果解读：

- 理论最大放行次数与实际放行次数完全一致
- `over_issued=0.00`，本次压测未出现超发
- 热点 key 场景能够覆盖 Redis Lua 原子扣减路径的并发竞争

## 验证快照

| 检查项 | 结果 |
| --- | --- |
| 令牌桶功能验证 | PASS |
| Redis 故障降级验证 | PASS |
| FastAPI `/healthz` | PASS |
| FastAPI `/metrics` | PASS |
| `pytest` 集成测试 | `5 passed in 71.06s` |
| 限流有效性断言 | PASS |

## 复现命令

```bash
docker compose build
docker compose up -d redis

docker compose run --rm test remote
docker compose run --rm -e REDIS_HOST=redis-unavailable test fallback
docker compose run --rm pytest

docker compose run --rm bench --workers 4 --duration 5
docker compose run --rm bench --workers 4 --duration 5 --shared-key

docker compose run --rm bench \
  --mode effectiveness \
  --workers 4 \
  --duration 5 \
  --shared-key \
  --max-tokens 20 \
  --refill-rate 5 \
  --max-over-issue 0 \
  --max-over-issue-ratio 0
```

## 总结

这个项目已经不仅仅是“实现了一个限流算法”，而是具备了：

- Redis 分布式配额共享
- Lua 原子更新
- Python/FastAPI 业务接入
- Redis 故障降级
- 自动化测试与 CI
- 压测与有效性断言

从秋招项目角度看，这份报告可以直接用来支撑“性能结果”和“限流有效性”两类面试问题。
