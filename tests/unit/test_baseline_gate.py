from backend.api.routes_baseline import MAX_TIMEOUT_SECONDS


def test_the_baseline_timeout_is_capped():
    assert MAX_TIMEOUT_SECONDS == 30.0