"""In-process sliding-window limits for the Telegram self-service routes.

The Subscription API runs a single replica (infra/subscription.bicep caps it
at one), so process memory is an exact count. If it is ever scaled out, each
replica limits independently, which loosens the limits by the replica count
but never removes them. Keys are bounded so a flood of distinct senders cannot
exhaust memory: the least recently used key is forgotten first.
"""

import time
from collections import OrderedDict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10000, clock=time.monotonic):
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("invalid rate limit")
        self.limit, self.window, self.max_keys, self.clock = limit, window_seconds, max_keys, clock
        self.events: OrderedDict = OrderedDict()

    def allow(self, key) -> bool:
        now = self.clock()
        history = self.events.pop(key, None) or deque()
        while history and history[0] <= now - self.window:
            history.popleft()
        allowed = len(history) < self.limit
        if allowed:
            history.append(now)
        self.events[key] = history
        while len(self.events) > self.max_keys:
            self.events.popitem(last=False)
        return allowed
