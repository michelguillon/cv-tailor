"""tests/conftest.py — session-wide test fixtures.

**Tracing off for the whole suite.** `docker-compose.yml` loads `.env` (which carries the
real `LANGFUSE_PUBLIC_KEY`) into the `cli` container, so without this the suite would run
*traced* — exporting mock-data spans to the production Langfuse server and coupling tests to
its reachability. The Langfuse instrumentation is opt-in by `LANGFUSE_PUBLIC_KEY`
(`tailor/telemetry.is_enabled`), so unsetting it once here makes every `telemetry.*` call a
clean no-op for the run — matching the instrumentation spec's "tests run with no key" contract.

Escape hatch: set `CV_TAILOR_TRACE_TESTS=1` to KEEP the key and run the suite *traced* — used
to validate the enabled instrumentation path end-to-end against a real Langfuse server. Off by
default so normal runs (incl. CI / the deployed container) stay untraced.
"""

import os

if os.getenv("CV_TAILOR_TRACE_TESTS") != "1":
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
