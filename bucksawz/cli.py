import click
import json
from .report.render import render
from .schema.infracost import InfracostOutput
from .schema.html_parser import parse_html


def _load_estimates(path: str) -> dict[str, float] | None:
    """Extract estimatedMonthlyCost entries from an enriched JSON file, if present."""
    try:
        with open(path) as f:
            data = json.load(f)
        estimates = {}
        for p in data.get("projects", []):
            for r in p.get("resources", []):
                est = r.get("estimatedMonthlyCost")
                if est is not None:
                    estimates[r["name"]] = float(est)
        return estimates or None
    except Exception:
        return None


@click.group()
def cli():
    """bucksawz — cloud cost reporting and estimation."""
    pass


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to infracost JSON output")
@click.option("--output", "-o", "output_path", default="report.html", show_default=True, help="Output HTML path")
def report(input_path, output_path):
    """Generate a rich HTML cost report from infracost JSON output.

    If the input is an enriched JSON (produced by `bucksawz enrich`), cost
    estimates from CloudWatch actuals are automatically shown in the report.
    """
    output = InfracostOutput.from_file(input_path)
    estimates = _load_estimates(input_path)
    render(output, output_path, estimates=estimates)


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to infracost JSON output")
@click.option("--output", "-o", "output_path", default="enriched.json", show_default=True)
@click.option("--lookback-days", default=90, show_default=True, help="Days of Cost Explorer history to pull")
@click.option("--aws-profile", default=None, help="AWS profile name")
@click.option("--aws-region", default="us-east-1", show_default=True)
@click.option("--cache-ttl", default=7, show_default=True, help="Cache TTL in days (default 7)")
@click.option("--force-refresh", is_flag=True, default=False, help="Bypass cache and re-fetch from AWS")
@click.option("--no-cloudwatch", is_flag=True, default=False, help="Skip CloudWatch metric enrichment")
def enrich(input_path, output_path, lookback_days, aws_profile, aws_region, cache_ttl, force_refresh, no_cloudwatch):
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
        cloudwatch=not no_cloudwatch,
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


@cli.group()
def prices():
    """Manage the local AWS Pricing API cache (~/.cache/bucksawz/prices.db)."""
    pass


@prices.command("update")
@click.option(
    "--services", "-s", default=",".join(["ECS", "Lambda", "EC2", "RDS"]),
    show_default=True,
    help="Comma-separated list of services to update (ECS, Lambda, EC2, RDS).",
)
@click.option(
    "--regions", "-r", default="us-east-1",
    show_default=True,
    help="Comma-separated list of AWS regions to fetch prices for.",
)
@click.option("--aws-profile", default=None, help="AWS profile name.")
def prices_update(services, regions, aws_profile):
    """Fetch current AWS prices and store them in the local SQLite price cache.

    Requires pricing:GetProducts IAM permission.
    Prices are fetched from the AWS Pricing API (global endpoint, us-east-1).
    """
    from .pricing.fetcher import fetch_all, ALL_SERVICES
    svc_list = [s.strip() for s in services.split(",") if s.strip()]
    region_list = [r.strip() for r in regions.split(",") if r.strip()]
    click.echo(f"Fetching prices for services: {', '.join(svc_list)}")
    click.echo(f"Regions: {', '.join(region_list)}")
    totals = fetch_all(region_list, svc_list, profile=aws_profile)
    click.echo("\nDone.")
    for svc, n in totals.items():
        click.echo(f"  {svc}: {n} price records stored")


@prices.command("info")
def prices_info():
    """Show price DB location and row counts per service/region."""
    from .pricing.db import db_path, count, service_summary
    p = db_path()
    click.echo(f"Price DB: {p}")
    if not p.exists():
        click.echo("  (empty — run `bucksawz prices update` to populate)")
        return
    click.echo(f"Total rows: {count()}")
    for row in service_summary():
        click.echo(
            f"  {row['service']:<20} {row['region']:<16} {row['rows']:>5} rows  "
            f"last fetched: {row['last_fetched'][:19]}"
        )


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
