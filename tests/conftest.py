"""Shared test fixtures.

rigsmith keeps several process-wide caches - settings, the draft store, the
model catalog, the job manager - because every surface needs cheap access to
them. That is right for a running tool and wrong for a test suite: a test
that extracts a verified catalog would otherwise change how validation
behaves in every test that runs after it, which is exactly the kind of
order-dependent failure that wastes an afternoon.

So all of it is reset around every test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_global_state():
    """Clear every process-wide cache before and after each test."""

    def clear() -> None:
        from rigsmith import config
        from rigsmith.core import drafts, jobs
        from rigsmith.rig import catalog

        config.set_settings(None)
        drafts.set_store(None)
        jobs.set_manager(None)
        catalog._cached = None

    clear()
    yield
    clear()
