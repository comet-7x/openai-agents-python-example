"""A minimal synchronous publish/subscribe bus.

Producers call `publish(event)`; every subscriber handler is invoked in turn.
Publishing is synchronous and cheap — handlers just mutate a view-model or append
a log line — so it is safe to call from both async hooks and sync tool methods.

A handler that raises must not take down the system that published the event, so
each handler call is exception-isolated. With no subscribers, `publish` is nearly
free, which is why agents can always publish unconditionally.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from comet.message_bus.events import Event

Subscriber = Callable[[Event], None]


class MessageBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, handler: Subscriber) -> Callable[[], None]:
        """Register `handler`. Returns an unsubscribe callable."""
        self._subscribers.append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        # Iterate a copy so a handler may (un)subscribe during dispatch.
        for handler in list(self._subscribers):
            # A misbehaving sink must never break the producer.
            with contextlib.suppress(Exception):
                handler(event)
