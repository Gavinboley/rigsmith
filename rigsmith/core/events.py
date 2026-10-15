"""A small publish/subscribe bus for things the page should see as they happen.

Jobs already report progress; a chat produces a different kind of stream -
a model's text, each tool it calls and what came back, a request for your
approval - and the web socket forwards whatever is published here. Thread
safe, in-process, and nothing waits on a subscriber.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

_subscribers: list[Callable[[dict], None]] = []
_lock = threading.Lock()


def subscribe(callback: Callable[[dict], None]) -> Callable[[], None]:
    with _lock:
        _subscribers.append(callback)

    def unsubscribe() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return unsubscribe


def publish(event: dict) -> None:
    event = {"at": time.time(), **event}
    with _lock:
        targets = list(_subscribers)
    for callback in targets:
        try:
            callback(event)
        except Exception:  # a broken listener must not break the publisher
            continue


def reset() -> None:
    with _lock:
        _subscribers.clear()
