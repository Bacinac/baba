"""Settings → System draws a red warning per service off `last_error`.

Nothing ever set it, so every service reported "no error, ever" — a health
panel that structurally cannot show ill health, which reads as all-clear.
"""

import logging

from baba_core.stats import StatsCollector


def test_the_last_error_a_service_hit_reaches_its_snapshot():
    stats = StatsCollector(service="test-service")
    assert stats.snapshot().last_error is None

    logging.getLogger("baba.somewhere").error("the decoder went away")

    snap = stats.snapshot()
    assert snap.last_error == "baba.somewhere: the decoder went away"
    assert snap.last_error_ns > 0


def test_a_warning_is_not_an_error():
    stats = StatsCollector(service="test-service-2")
    logging.getLogger("baba.somewhere").warning("a plate was discarded")
    assert stats.snapshot().last_error is None


def test_listening_for_errors_does_not_cost_the_service_its_console():
    """`basicConfig` is a no-op once the root has any handler, so a collector
    built before `setup_logging` used to leave the service with no output at
    all — silence that looks exactly like a quiet night."""
    from baba_core.logconfig import setup_logging

    root = logging.getLogger()
    saved, root.handlers = root.handlers, []
    try:
        StatsCollector(service="test-service-3")
        setup_logging("test-service-3")
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    finally:
        root.handlers = saved
