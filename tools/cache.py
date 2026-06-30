"""
Execution-level in-memory cache.

- Lives for the entire program execution
- Single-thread safe (no locks needed)
- Fast O(1) lookups
- Ideal for ETL / batch jobs
"""

from typing import Any, Callable


class ExecutionCache:
    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        """Get a value from cache. Returns None if not found."""
        return self._cache.get(key)

    def set(self, key: str, value: Any) -> None:
        """Set a value in cache."""
        self._cache[key] = value

    def get_or_set(self, key: str, loader: Callable[[], Any]) -> Any:
        """
        Get value from cache.
        If not present, compute using loader() and store it.
        """
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def exists(self, key: str) -> bool:
        return key in self._cache

    def invalidate(self, key: str) -> None:
        """Remove a single key from the cache so it is reloaded on next access."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear entire cache."""
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)


# 🔹 Singleton instance (import this everywhere)
execution_cache = ExecutionCache()
