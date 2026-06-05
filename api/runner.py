"""api/runner.py — run the pipeline in a background thread, streaming to a Session.

`run_pipeline` is blocking and emits progress via its `on_event` hook; we run it off
the event loop in a daemon thread and feed every event straight into the session's
buffer (which wakes the SSE stream). Durable artifacts still land in outputs/<run_id>/
exactly as a CLI run; the session only carries the live event stream + final summary.

UI Step 3 uses AutoHITL (the run goes start-to-finish); UI Step 4 swaps in an
SSE/HITL handler that pauses at each checkpoint via Session.wait_hitl.
"""

from __future__ import annotations

import threading

from tailor.run import AutoHITL, PipelineStop, run_pipeline

__all__ = ["launch_run"]


def launch_run(store, session, jd_text, *, mode="demo", key=None, max_iterations=None,
               output_dir="outputs") -> threading.Thread:
    """Write the JD to the session's tmp dir and run the pipeline in a daemon thread.

    Terminal status is set by the thread: complete (run_pipeline returned), stopped
    (PipelineStop — e.g. no_fit), or error (anything else). run_pipeline emits its own
    run_complete / stopped event before returning/raising, so the SSE stream sees it."""
    jd_path = store.base_dir / session.run_id / "jd.txt"
    jd_path.parent.mkdir(parents=True, exist_ok=True)
    jd_path.write_text(jd_text, encoding="utf-8")

    def target() -> None:
        try:
            session.set_status("running")
            summary = run_pipeline(
                str(jd_path), mode=mode, key=key, max_iterations=max_iterations,
                output_dir=output_dir, hitl=AutoHITL(), run_id=session.run_id,
                on_event=session.add_event,
            )
            session.result = summary
            session.set_status("complete")
        except PipelineStop as exc:                # no_fit (AutoHITL didn't override)
            session.error = str(exc)
            session.set_status("stopped")          # run_pipeline already emitted 'stopped'
        except Exception as exc:                   # surface any failure to the stream
            session.error = str(exc)
            session.add_event({"type": "error", "message": str(exc)})
            session.set_status("error")

    thread = threading.Thread(target=target, name=f"run-{session.run_id}", daemon=True)
    thread.start()
    return thread
