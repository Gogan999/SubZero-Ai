import logging
import os

# Expected failures (Slack down, Claude errors) log tracebacks on purpose. Keep test output readable.
if not os.environ.get("SUBZERO_TEST_LOGS"):
    logging.disable(logging.CRITICAL)

# The Fake sandbox (no isolation) is allowed only while testing; see config.fake_sandbox_allowed
os.environ.setdefault("SUBZERO_TESTING_FAKE_SANDBOX", "1")
