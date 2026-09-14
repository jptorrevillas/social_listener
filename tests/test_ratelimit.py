from social_listener.ratelimit import RateLimiter


def make(qpm=100, headroom=0.15):
    slept = []
    limiter = RateLimiter(qpm=qpm, headroom=headroom, clock=lambda: 0.0, sleeper=slept.append)
    return limiter, slept


def test_falls_back_to_configured_qpm_before_any_response():
    limiter, slept = make()
    limiter.acquire()
    limiter.acquire()
    # 100 qpm less 15% headroom = 85/min = one call per 0.706s.
    assert round(slept[-1], 3) == round(60 / 85, 3)


def test_headers_override_the_configured_rate():
    limiter, slept = make()
    limiter.acquire()
    limiter.observe(
        {"x-ratelimit-remaining": "20", "x-ratelimit-reset": "100", "x-ratelimit-used": "80"}
    )
    limiter.acquire()
    # 20 remaining less headroom, spread over 100s.
    assert round(slept[-1], 2) == round(100 / (20 * 0.85), 2)


def test_exhausted_budget_waits_out_the_window():
    limiter, slept = make()
    limiter.acquire()
    limiter.observe({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "42"})
    limiter.acquire()
    assert slept[-1] >= 42


def test_backoff_prefers_the_reset_hint():
    limiter, _ = make()
    limiter.observe({"x-ratelimit-reset": "30"})
    assert limiter.backoff_seconds(1) == 30.0


def test_backoff_is_exponential_without_a_hint():
    limiter, _ = make()
    assert limiter.backoff_seconds(1) == 2.0
    assert limiter.backoff_seconds(3) == 8.0


def test_malformed_headers_are_ignored():
    limiter, _ = make()
    limiter.observe({"x-ratelimit-remaining": "not-a-number"})
    assert limiter.state.remaining is None


def test_missing_headers_leave_state_untouched():
    limiter, _ = make()
    limiter.observe({})
    assert limiter.state.is_known is False
