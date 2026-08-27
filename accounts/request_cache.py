"""Request-local memoization for repeated access and hierarchy lookups.

The cache exists only for the lifetime of one HTTP request.  It therefore
removes duplicate permission queries across decorators, views, serializers and
template context processors without making permission changes eventually
consistent or dependent on a process-global cache.
"""

from contextvars import ContextVar
from typing import Any, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction


_request_cache: ContextVar[dict | None] = ContextVar(
    "workspace_request_cache",
    default=None,
)


def request_cached(key: tuple, factory: Callable[[], Any]) -> Any:
    """Resolve ``key`` once when called inside the current HTTP request."""

    cache = _request_cache.get()
    if cache is None:
        return factory()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


class RequestAccessCacheMiddleware:
    """Create and reliably clear one memoization dictionary per request."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self._is_async:
            return self.__acall__(request)

        token = _request_cache.set({})
        try:
            return self.get_response(request)
        finally:
            _request_cache.reset(token)

    async def __acall__(self, request):
        token = _request_cache.set({})
        try:
            return await self.get_response(request)
        finally:
            _request_cache.reset(token)
