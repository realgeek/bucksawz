import click
from .report.render import render
from .schema.infracost import InfracostOutput
from .schema.html_parser import parse_html


@click.group()
def cli():
    """bucksawz — cloud cost reporting and estimation."""
    pass


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to infracost JSON output")
@click.option("--output", "-o", "output_path", default="report.html", show_default=True, help="Output HTML path")
def report(input_path, output_path):
    """Generate a rich HTML cost report from infracost JSON output."""
    output = InfracostOutput.from_file(input_path)
    render(output, output_path)


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to infracost JSON output")
@click.option("--output", "-o", "output_path", default="enriched.json", show_default=True)
@click.option("--lookback-days", default=90, show_default=True, help="Days of Cost Explorer history to pull")
@click.option("--aws-profile", default=None, help="AWS profile name")
@click.option("--aws-region", default="us-east-1", show_default=True)
@click.option("--cache-ttl", default=7, show_default=True, help="Cache TTL in days (default 7)")
@click.option("--force-refresh", is_flag=True, default=False, help="Bypass cache and re-fetch from AWS")
def enrich(input_path, output_path, lookback_days, aws_profile, aws_region, cache_ttl, force_refresh):
    """Enrich infracost JSON with AWS Cost Explorer actuals.

    Results are cached in ~/.cache/bucksawz/ for --cache-ttl days (default 7).
    Use --force-refresh to bypass the cache.
    """
    from .aws.costexplorer import enrich_output
    import json
    output = InfracostOutput.from_file(input_path)
    enriched = enrich_output(
        output,
        lookback_days=lookback_days,
        profile=aws_profile,
        region=aws_region,
        cache_ttl_days=cache_ttl,
        force_refresh=force_refresh,
    )
    with open(output_path, "w") as f:
        json.dump(enriched, f, indent=2)
    click.echo(f"Enriched output written to {output_path}")


@cli.group()
def cache():
    """Manage the local pricing/Cost Explorer cache (~/.cache/bucksawz/)."""
    pass


@cache.command("clear")
@click.option("--all", "clear_all", is_flag=True, default=False, help="Clear all entries, not just expired")
def cache_clear(clear_all):
    """Remove expired (or all) cache entries."""
    from .aws.cache import clear_expired, _CACHE_DIR
    import shutil
    if clear_all:
        if _CACHE_DIR.exists():
            shutil.rmtree(_CACHE_DIR)
            click.echo(f"Cleared all cache entries in {_CACHE_DIR}")
        else:
            click.echo("Cache directory does not exist.")
    else:
        removed = clear_expired()
        click.echo(f"Removed {removed} expired cache entries.")


@cache.command("info")
def cache_info():
    """Show cache directory location and entry count."""
    from .aws.cache import _CACHE_DIR
    import json as _json
    from datetime import datetime, timezone
    if not _CACHE_DIR.exists():
        click.echo(f"Cache: {_CACHE_DIR} (empty)")
        return
    entries = list(_CACHE_DIR.glob("*.json"))
    now = datetime.now(timezone.utc)
    click.echo(f"Cache: {_CACHE_DIR}")
    click.echo(f"Entries: {len(entries)}")
    for p in sorted(entries):
        try:
            env = _json.loads(p.read_text())
            age = now - datetime.fromisoformat(env["cached_at"])
            click.echo(f"  {p.name}  age={age.days}d  key={env.get('key','?')[:60]}")
        except Exception:
            click.echo(f"  {p.name}  (unreadable)")


@cli.command("from-html")
@click.argument("html_path")
@click.option("--output", "-o", "output_path", default="report.html", show_default=True, help="Output HTML path")
def from_html(html_path, output_path):
    """Generate a rich HTML report directly from an existing infracost HTML report."""
    output = parse_html(html_path)
    render(output, output_path)


@cli.command("html-to-json")
@click.argument("html_path")
@click.option("--output", "-o", "output_path", default="infracost.json", show_default=True)
def html_to_json(html_path, output_path):
    """Convert an existing infracost HTML report to JSON (for inspection or re-processing)."""
    import json, dataclasses
    output = parse_html(html_path)
    # Simple dataclass → dict serialisation
    def _ser(obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        raise TypeError(f"Not serialisable: {type(obj)}")
    with open(output_path, "w") as f:
        json.dump(dataclasses.asdict(output), f, indent=2, default=str)
    click.echo(f"JSON written to {output_path}")
