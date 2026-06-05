"""api/ — FastAPI backend for the cv-tailor Web UI (SPEC §12).

Imports the tailor package directly (never subprocess — RFI entry 15/16): the same
pipeline the CLI runs. Three routers (corpus / runs / hitl) over an in-memory
SessionStore; pipeline artifacts persist in outputs/<run_id>/ exactly as in CLI runs.
"""
