"""go2rtc has no open mode.

It runs network_mode:host, so its API and RTSP listen on every interface, and
/api/config answers with every camera's password. A client that started without
credentials would leave all of that to the LAN, so it does not start.
"""

import pytest
from baba_core import go2rtc_auth_from_env


def test_without_a_password_nothing_starts(monkeypatch):
    monkeypatch.delenv("BABA_GO2RTC_API_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="BABA_GO2RTC_API_PASSWORD is required"):
        go2rtc_auth_from_env()


def test_the_pair_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BABA_GO2RTC_API_PASSWORD", "s3cret")
    monkeypatch.delenv("BABA_GO2RTC_API_USER", raising=False)
    assert go2rtc_auth_from_env() == ("baba", "s3cret")
    monkeypatch.setenv("BABA_GO2RTC_API_USER", "nvr")
    assert go2rtc_auth_from_env() == ("nvr", "s3cret")
