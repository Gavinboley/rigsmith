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
        from rigsmith.core import appsettings, drafts, jobs
        from rigsmith.loop import backend as audio_backend
        from rigsmith.loop import virtual
        from rigsmith.match import source as audio_source
        from rigsmith.rig import catalog

        config.set_settings(None)
        drafts.set_store(None)
        jobs.set_manager(None)
        catalog._cached = None
        # Audio inspections are remembered by path, size and mtime. pytest
        # reuses tmp_path names, so a stale entry there would be a very
        # confusing afternoon.
        audio_source.forget_cached()
        # The simulated bench keeps knob positions and a random generator; a
        # test that turns the gain up must not hand that to the next one.
        virtual.set_world(None)
        audio_backend.set_backend(None)
        appsettings.reset_cache()
        # Whether this machine's Claude Code is signed in is not the suite's
        # business: pin the answer to "cannot say" rather than run `claude`.
        from rigsmith.agent import providers

        providers._signed_in_cache.clear()
        providers._signed_in_cache.update(value=(None,), at=float("inf"))

    clear()
    yield
    clear()


@pytest.fixture(autouse=True, scope="session")
def test_client_host():
    """FastAPI's test client calls itself "testserver"; the web guard refuses
    any Host that is not this machine, so that name is let in - here only."""
    from rigsmith.web import guard

    guard.EXTRA_HOSTS.add("testserver")
    yield
    guard.EXTRA_HOSTS.discard("testserver")
