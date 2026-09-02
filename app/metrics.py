from __future__ import annotations

from app.cache import cache

# simple in-memory metrics for perf harness
metrics = {
    "requests_total": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "upstream_fetches": 0,
    "synthesis_count": 0,
    "metadata_heads": 0,
    "metadata_enrichments": 0,
    "metadata_enrichment_dropped": 0,
    "metadata_extractions": 0,
    "metadata_extraction_failures": 0,
    "metadata_recovery_attempts": 0,
    "metadata_recovery_successes": 0,
    "metadata_recovery_failures": 0,
    "metadata_recovery_native_fallbacks": 0,
}

_BACKEND_CODES = {"memory": 0, "connected": 1, "degraded": 2, "disconnected": 3}


def prometheus_text() -> str:
    backend_code = _BACKEND_CODES.get(cache.state.value, -1)
    lines = [
        f"proxy_requests_total {metrics['requests_total']}",
        "# HELP proxy_cache_hits Python project-cache hits served by memory or Redis.",
        f"proxy_cache_hits {metrics['cache_hits']}",
        "# HELP proxy_cache_misses Python project-cache misses; this does not describe nginx artifact caching.",
        f"proxy_cache_misses {metrics['cache_misses']}",
        f"proxy_upstream_fetches {metrics['upstream_fetches']}",
        f"proxy_synthesis_count {metrics['synthesis_count']}",
        f"proxy_metadata_heads_total {metrics['metadata_heads']}",
        f"proxy_metadata_enrichments_total {metrics['metadata_enrichments']}",
        f"proxy_metadata_enrichment_dropped_total {metrics['metadata_enrichment_dropped']}",
        f"proxy_metadata_extractions_total {metrics['metadata_extractions']}",
        f"proxy_metadata_extraction_failures_total {metrics['metadata_extraction_failures']}",
        f"proxy_metadata_recovery_attempts_total {metrics['metadata_recovery_attempts']}",
        f"proxy_metadata_recovery_successes_total {metrics['metadata_recovery_successes']}",
        f"proxy_metadata_recovery_failures_total {metrics['metadata_recovery_failures']}",
        f"proxy_metadata_recovery_native_fallbacks_total {metrics['metadata_recovery_native_fallbacks']}",
        f"proxy_cache_redis_errors_total {cache.redis_errors}",
        f"proxy_cache_redis_probe_errors_total {cache.redis_probe_errors}",
        f"proxy_cache_backend {backend_code}",
    ]
    return "\n".join(lines)
