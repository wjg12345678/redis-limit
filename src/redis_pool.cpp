#include "redis_pool.hpp"

#include <stdexcept>

namespace rrl {

RedisPool::RedisPool(const RedisConfig& config) : config_(config) {
    if (config_.pool_size <= 0) {
        throw std::invalid_argument("Redis pool size must be positive");
    }

    for (int i = 0; i < config_.pool_size; ++i) {
        auto conn = create_connection();
        if (conn.is_valid()) {
            pool_.push(std::move(conn));
            stats_.total_connections++;
        }
    }

    maintainer_thread_ = std::thread(&RedisPool::maintain_pool, this);
}

RedisPool::~RedisPool() {
    shutdown_ = true;
    cv_.notify_all();
    if (maintainer_thread_.joinable()) {
        maintainer_thread_.join();
    }

    std::lock_guard<std::mutex> lock(pool_mutex_);
    while (!pool_.empty()) {
        pool_.pop();
    }
}

RedisConnection RedisPool::create_connection() {
    struct timeval tv;
    tv.tv_sec = config_.connect_timeout_ms / 1000;
    tv.tv_usec = (config_.connect_timeout_ms % 1000) * 1000;

    redisContext* ctx = redisConnectWithTimeout(config_.host.c_str(), config_.port, tv);
    if (!ctx || ctx->err) {
        if (ctx) {
            redisFree(ctx);
        }
        return RedisConnection(nullptr);
    }

    tv.tv_sec = config_.socket_timeout_ms / 1000;
    tv.tv_usec = (config_.socket_timeout_ms % 1000) * 1000;
    if (redisSetTimeout(ctx, tv) != REDIS_OK) {
        redisFree(ctx);
        return RedisConnection(nullptr);
    }

    if (!config_.password.empty()) {
        auto* reply = static_cast<redisReply*>(redisCommand(ctx, "AUTH %s", config_.password.c_str()));
        const bool ok = reply && reply->type != REDIS_REPLY_ERROR;
        if (reply) {
            freeReplyObject(reply);
        }
        if (!ok) {
            redisFree(ctx);
            return RedisConnection(nullptr);
        }
    }

    if (config_.db != 0) {
        auto* reply = static_cast<redisReply*>(redisCommand(ctx, "SELECT %d", config_.db));
        const bool ok = reply && reply->type != REDIS_REPLY_ERROR;
        if (reply) {
            freeReplyObject(reply);
        }
        if (!ok) {
            redisFree(ctx);
            return RedisConnection(nullptr);
        }
    }

    return RedisConnection(ctx);
}

RedisConnection RedisPool::acquire(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(pool_mutex_);
    stats_.total_requests++;
    auto deadline = std::chrono::steady_clock::now() + timeout;
    while (pool_.empty() && !shutdown_) {
        stats_.wait_count++;
        if (cv_.wait_until(lock, deadline) == std::cv_status::timeout) {
            stats_.failed_requests++;
            return RedisConnection(nullptr);
        }
    }

    if (shutdown_) {
        stats_.failed_requests++;
        return RedisConnection(nullptr);
    }

    auto conn = std::move(pool_.front());
    pool_.pop();
    stats_.active_connections++;

    if (!conn.is_valid()) {
        conn = create_connection();
        if (!conn.is_valid()) {
            stats_.failed_requests++;
            stats_.active_connections--;
            return RedisConnection(nullptr);
        }
    }

    return conn;
}

void RedisPool::release(RedisConnection conn) {
    if (!conn.is_valid()) {
        stats_.active_connections--;
        stats_.failed_requests++;
        return;
    }

    std::lock_guard<std::mutex> lock(pool_mutex_);
    pool_.push(std::move(conn));
    stats_.active_connections--;
    cv_.notify_one();
}

PoolStatsSnapshot RedisPool::get_stats() const {
    PoolStatsSnapshot snapshot;
    snapshot.total_connections = stats_.total_connections.load();
    snapshot.active_connections = stats_.active_connections.load();
    snapshot.wait_count = stats_.wait_count.load();
    snapshot.total_requests = stats_.total_requests.load();
    snapshot.failed_requests = stats_.failed_requests.load();
    return snapshot;
}

bool RedisPool::health_check() {
    std::lock_guard<std::mutex> lock(pool_mutex_);
    std::queue<RedisConnection> temp;
    bool healthy = true;

    while (!pool_.empty()) {
        auto conn = std::move(pool_.front());
        pool_.pop();

        if (conn.is_valid()) {
            auto reply = conn.execute("PING");
            if (reply && reply->type == REDIS_REPLY_STATUS &&
                reply->str != nullptr &&
                std::string(reply->str) == "PONG") {
                temp.push(std::move(conn));
            } else {
                stats_.total_connections--;
                healthy = false;
            }
        } else {
            stats_.total_connections--;
            healthy = false;
        }
    }

    while (temp.size() < static_cast<size_t>(config_.pool_size)) {
        auto conn = create_connection();
        if (conn.is_valid()) {
            temp.push(std::move(conn));
            stats_.total_connections++;
        } else {
            healthy = false;
            break;
        }
    }

    if (temp.size() < static_cast<size_t>(config_.pool_size)) {
        healthy = false;
    }

    pool_ = std::move(temp);
    return healthy;
}

void RedisPool::resize(size_t new_size) {
    std::lock_guard<std::mutex> lock(pool_mutex_);
    if (new_size == 0) {
        throw std::invalid_argument("Redis pool size must be positive");
    }

    if (new_size > pool_.size()) {
        const size_t need = new_size - pool_.size();
        for (size_t i = 0; i < need; ++i) {
            auto conn = create_connection();
            if (conn.is_valid()) {
                pool_.push(std::move(conn));
                stats_.total_connections++;
            }
        }
    } else if (new_size < pool_.size()) {
        const size_t remove = pool_.size() - new_size;
        for (size_t i = 0; i < remove; ++i) {
            pool_.pop();
            stats_.total_connections--;
        }
    }

    config_.pool_size = static_cast<int>(new_size);
}

void RedisPool::maintain_pool() {
    while (!shutdown_) {
        std::this_thread::sleep_for(std::chrono::seconds(30));

        if (shutdown_) {
            break;
        }

        health_check();
    }
}

} // namespace rrl
