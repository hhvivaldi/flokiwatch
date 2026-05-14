"""FLO-426 — verify billing-exhausted errors are classified as non-retryable.

Reproduces the substring-match logic from ai_agent.py:_call_openai_with_tools
exception handler and asserts the canonical Anthropic phrase + variants
trigger non-retryable, while genuinely transient errors do not.

Run: python test_flo426_billing_error_detection.py
"""

import sys


# Mirror of ai_agent.py:_call_openai_with_tools exception classifier — keep
# in lockstep with the patch. If this duplicates and drifts, the production
# fix is the source of truth; update the test.
def classify(err_str: str) -> dict:
    s = err_str.lower()
    billing_exhausted = any(k in s for k in (
        "credit balance is too low",
        "credit balance too low",
        "credits required",
        "402",
        "billing.*upgrade",
    ))
    non_retryable = billing_exhausted or any(k in s for k in (
        "arrearage", "overdue-payment", "access denied",
        "insufficient_quota", "invalid_api_key", "invalid api key",
        "unauthorized", "forbidden", "451",
    ))
    return {"billing_exhausted": billing_exhausted, "non_retryable": non_retryable}


# Real error string captured from logs/trading_bot_2026-05-14.log:15:37:33
ANTHROPIC_CREDIT_EXHAUSTED = (
    "Error code: 400 - {'type': 'error', 'error': "
    "{'type': 'invalid_request_error', 'message': "
    "'Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits.'}, "
    "'request_id': 'req_011Cb2Yyt5Dv4QtKRL9gUMhh'}"
)

# Real OpenAI billing variant (per stripe/billing docs)
OPENAI_QUOTA = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current "
    "quota, please check your plan and billing details.', "
    "'type': 'insufficient_quota', 'param': null, 'code': 'insufficient_quota'}}"
)

# 402 from a hypothetical pay-per-use proxy
PAYMENT_REQUIRED = "Error code: 402 - Payment Required: top up credits to continue"

# Genuinely transient errors that MUST stay retryable
TRANSIENT_500 = "Error code: 500 - {'error': {'message': 'overloaded_error'}}"
TRANSIENT_503 = "Error code: 503 - Service Unavailable"
TRANSIENT_TIMEOUT = "TimeoutError: request timed out after 90s"
TRANSIENT_CONN_RESET = "ConnectionResetError: [Errno 104] Connection reset by peer"


cases = [
    # (label, err_str, expected_non_retryable, expected_billing)
    ("anthropic_credit_balance_too_low", ANTHROPIC_CREDIT_EXHAUSTED, True,  True),
    ("openai_insufficient_quota",        OPENAI_QUOTA,                True,  False),
    ("402_payment_required",             PAYMENT_REQUIRED,            True,  True),
    ("transient_500",                    TRANSIENT_500,               False, False),
    ("transient_503",                    TRANSIENT_503,               False, False),
    ("transient_timeout",                TRANSIENT_TIMEOUT,           False, False),
    ("transient_conn_reset",             TRANSIENT_CONN_RESET,        False, False),
]


def main():
    failed = []
    for label, err, want_nr, want_billing in cases:
        got = classify(err)
        ok = (got["non_retryable"] == want_nr) and (got["billing_exhausted"] == want_billing)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: non_retryable={got['non_retryable']} (want {want_nr}), "
              f"billing={got['billing_exhausted']} (want {want_billing})")
        if not ok:
            failed.append(label)

    print()
    if failed:
        print(f"FAILED: {failed}")
        return 1
    print(f"ALL {len(cases)} CASES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
