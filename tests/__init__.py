import logging
import os

# Expected failures (Slack down, Claude errors) log tracebacks on purpose. Keep test output readable.
if not os.environ.get("SUBZERO_TEST_LOGS"):
    logging.disable(logging.CRITICAL)
