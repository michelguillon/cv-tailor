"""Deterministic tests for tailor/helpers.call_with_retry (no API calls).

The retry wrapper is load-bearing (R-05): a transient 429 mid-run must not abort.
We exercise it with a fake provider exception shaped like mistralai 2.x's
SDKError (status/headers on `raw_response`), monkeypatching sleep so it's fast.
"""

import pytest

import tailor.helpers as helpers
from tailor.helpers import call_with_retry


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeSDKError(Exception):
    """Mimics mistralai 2.x SDKError: status/headers live on raw_response."""
    def __init__(self, status_code, headers=None):
        super().__init__(f"status {status_code}")
        self.raw_response = FakeResponse(status_code, headers)


class FlakyCall:
    """Raises a given error N times, then returns a sentinel."""
    def __init__(self, error, fail_times):
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return "ok"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(helpers.time, "sleep", lambda *_: None)


def test_retries_then_succeeds():
    flaky = FlakyCall(FakeSDKError(429), fail_times=2)
    assert call_with_retry(flaky, retryable_exc=FakeSDKError) == "ok"
    assert flaky.calls == 3


def test_non_retryable_4xx_raises_immediately():
    flaky = FlakyCall(FakeSDKError(400), fail_times=5)
    with pytest.raises(FakeSDKError):
        call_with_retry(flaky, retryable_exc=FakeSDKError)
    assert flaky.calls == 1  # no retry on a client-side bug


def test_gives_up_after_max_retries():
    flaky = FlakyCall(FakeSDKError(503), fail_times=99)
    with pytest.raises(FakeSDKError):
        call_with_retry(flaky, retryable_exc=FakeSDKError, max_retries=3)
    assert flaky.calls == 4  # initial + 3 retries


def test_all_retryable_statuses():
    for status in (429, 500, 502, 503, 504):
        flaky = FlakyCall(FakeSDKError(status), fail_times=1)
        assert call_with_retry(flaky, retryable_exc=FakeSDKError) == "ok"


def test_unexpected_exception_type_not_caught():
    """Only the declared retryable_exc family is retried; other errors propagate."""
    def boom(*a, **k):
        raise ValueError("programming error")
    with pytest.raises(ValueError):
        call_with_retry(boom, retryable_exc=FakeSDKError)


def test_honours_retry_after_header(monkeypatch):
    slept = []
    monkeypatch.setattr(helpers.time, "sleep", lambda s: slept.append(s))
    flaky = FlakyCall(FakeSDKError(429, headers={"retry-after": "7"}), fail_times=1)
    call_with_retry(flaky, retryable_exc=FakeSDKError)
    assert slept == [7.0]
