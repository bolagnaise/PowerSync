"""HA storage with a fixed deadline for continuously updated accounting data."""

from collections.abc import Callable
from typing import Any

from homeassistant.helpers.storage import Store


class CoalescingStore(Store):
    """Keep the first delayed deadline while serializing the latest state.

    HA's normal delayed save debounces until updates stop. Accounting and
    learning keep changing during normal polling, so they need bounded progress.
    HA still owns the timer, final-write listener, serialization and disk I/O.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending_data_func: Callable[[], Any] | None = None

    def async_delay_save(self, data_func: Callable[[], Any], delay: float = 0) -> None:
        pending = self._pending_data_func is not None
        self._pending_data_func = data_func
        if pending:
            return
        try:
            super().async_delay_save(self._consume_pending_data, delay)
        except BaseException:
            self._pending_data_func = None
            raise

    def _consume_pending_data(self) -> Any:
        data_func = self._pending_data_func
        # Release before serialization/I/O, including failures, so the next
        # update can schedule another attempt rather than becoming stuck.
        self._pending_data_func = None
        return data_func()

    async def async_save(self, data: Any) -> None:
        self._pending_data_func = None
        await super().async_save(data)

    async def async_remove(self) -> None:
        self._pending_data_func = None
        await super().async_remove()
