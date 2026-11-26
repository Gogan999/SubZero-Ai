import logging
import os

# Turns logging off unless SUBZERO_TEST_LOGS is set.
if not os.environ.get("SUBZERO_TEST_LOGS"):
    logging.disable(logging.CRITICAL)

# Allows the Fake sandbox (no isolation) while testing; see config.fake_sandbox_allowed
os.environ.setdefault("SUBZERO_TESTING_FAKE_SANDBOX", "1")
