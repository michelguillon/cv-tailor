"""tailor CLI — `python -m tailor`. SPEC §6. Step 8.

    python -m tailor run --jd PATH [--demo] [--key KEY] [--dry-run] [--yes]
    python -m tailor replay <run_id> [--reasoning]

All commands run inside the `cli` Docker service (project root bind-mounted, so
outputs persist on the host). Full mode is key-gated (§3.7); demo is the default
safe path (1 iteration, Haiku, cheap).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from tailor.audit import read_entries
from tailor.config import ConfigError
from tailor.run import AutoHITL, PipelineStop, run_pipeline


@click.group()
def cli():
    """Multi-model CV tailoring orchestrator."""


@cli.command()
@click.option("--jd", "jd_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Path to the job-description text file.")
@click.option("--demo", is_flag=True, help="Demo mode: 1 iteration, Haiku orchestrator, cheap.")
@click.option("--key", default=None, help="Full-mode passphrase (or set FULL_MODE_KEY).")
@click.option("--output-dir", default="outputs", help="Where to write outputs/<run_id>/.")
@click.option("--max-iterations", type=int, default=None, help="Override the mode's iteration cap.")
@click.option("--dry-run", is_flag=True, help="Parse JD + assess fit only; no drafting (D-09).")
@click.option("--yes", is_flag=True, help="Non-interactive: accept every HITL checkpoint.")
def run(jd_path, demo, key, output_dir, max_iterations, dry_run, yes):
    """Tailor a CV to a job description (the main workflow)."""
    mode = "demo" if demo else "full"
    hitl = AutoHITL() if yes else None
    try:
        summary = run_pipeline(jd_path, mode=mode, key=key, max_iterations=max_iterations,
                               output_dir=output_dir, dry_run=dry_run, hitl=hitl)
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    except PipelineStop as exc:
        click.echo(f"\n■ {exc}")
        return

    click.echo("\n─── Run complete ──────────────────────────────────────────")
    click.echo(f"  run_id:   {summary['run_id']}  ({summary['mode']})")
    click.echo(f"  outcome:  {summary.get('outcome')}")
    if summary.get("dry_run"):
        click.echo("  dry-run: stopped after fit assessment (no CV produced).")
        return
    click.echo(f"  converged: {summary['converged']} ({summary['convergence_reason']}) "
               f"in {summary['iterations']} iteration(s)")
    click.echo(f"  CV:       {summary['cv_md']}")
    click.echo(f"  Report:   {summary['cv_html']}")
    click.echo(f"  Cost:     ${summary['cost_estimated_usd']:.4f} (estimated)  {summary['cost_breakdown']}")
    if summary.get("cost_cap_exceeded"):
        click.secho("  ⚠ estimated cost exceeded the mode's cost cap", fg="yellow")


@cli.command()
@click.argument("run_id")
@click.option("--reasoning", is_flag=True, help="Include the full reasoning trace.")
@click.option("--output-dir", default="outputs", help="Where outputs/<run_id>/ lives.")
def replay(run_id, reasoning, output_dir):
    """Inspect a past run: summary, iteration scores, cost breakdown."""
    run_dir = Path(output_dir) / run_id
    if not run_dir.is_dir():
        raise click.ClickException(f"no run at {run_dir}")
    entries = read_entries(run_dir / "run_log.jsonl")
    footer = next((e for e in reversed(entries) if e.get("type") == "run_complete"), None)

    click.echo(f"─── Replay: {run_id} ──────────────────────────────────────")
    for name in ("phase0_jd_analysis", "phase1_fit_assessment"):
        p = run_dir / f"{name}.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if name == "phase0_jd_analysis":
                click.echo(f"  Role:    {data.get('role_title')} ({data.get('seniority_level')})")
            else:
                click.echo(f"  Fit:     {data.get('outcome')} ({data.get('overall_fit_score')})")

    iters = sorted(run_dir.glob("iteration_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    if iters:
        click.echo("  Iterations (coverage / quality):")
        for p in iters:
            it = json.loads(p.read_text(encoding="utf-8"))
            q = "—" if it.get("critique_score") is None else f"{it['critique_score']:.1f}"
            click.echo(f"    iter {it['iteration']}: {it['keyword_coverage']:.0%} / {q}  "
                       f"(Δkw {it['keyword_delta']:+.3f}, Δq {it['quality_delta']:+.2f})")

    if footer:
        click.echo(f"  Cost:    ${footer['total_estimated_usd']:.4f} estimated  "
                   f"{footer['cost_breakdown_estimated_usd']}")

    if reasoning:
        click.echo("\n─── Reasoning trace ───────────────────────────────────────")
        for e in entries:
            if e.get("type") == "run_complete":
                continue
            it = f" [iter {e['iteration']}]" if e.get("iteration") is not None else ""
            click.echo(f"  {e.get('phase')}{it} · {e.get('event')}: {e.get('reasoning')}")


if __name__ == "__main__":
    cli()
