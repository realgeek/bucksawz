import click
import json
from .report.render import render
from .schema.infracost import InfracostOutput
from .schema.html_parser import parse_html


def _load_enrichment(path: str) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    """
    Extract estimates and account breakdown from an enriched JSON file.
    Returns (estimates, account_breakdown) — either may be None if absent.
    estimates: {resource_name: estimated_monthly_cost}
    account_breakdown: {account_id: monthly_average_cost}
    """
    try:
        with open(path) as f:
            data = json.load(f)
        estimates: dict[str, float] = {}
        for p in data.get("projects", []):
            for r in p.get("resources", []):
                est = r.get("estimatedMonthlyCost")
                if est is not None:
                    estimates[r["name"]] = float(est)
        hist = data.get("historical", {})
        account_breakdown: dict[str, float] = hist.get("monthlyAverageByAccount", {})
        return estimates or None, account_breakdown or None
    except Exception:
        return None, None


@click.group()
def cli():
    """bucksawz — cloud cost reporting and estimation."""
    pass


_support_plan_option = click.option(
    "--support-plan",
    type=click.Choice(["developer", "business", "enterprise-onramp", "enterprise"]),
    default=None,
    help="Add an AWS Support line: a tiered percentage of the priced total, and of "
         "the plan delta. Omit to report infrastructure cost only.",
)


@cli.command()
@click.option("--input", "-i", "input_path", required=True, help="Path to infracost JSON output")
@click.option("--output", "-o", "output_path", default="report.html", show_default=True, help="Output HTML path")
@_support_plan_option
def report(input_path, output_path, support_plan):
    """Generate a rich HTML cost report from infracost JSON output.

    If the input is an enriched JSON (produced by `bucksawz enrich`), cost
    estimates from CloudWatch actuals are automatically shown in the report.
    """
    output = InfracostOutput.from_file(input_path)
    estimates, account_breakdown = _load_enrichment(input_path)
    render(
        output, output_path,
        estimates=estimates,
        account_breakdown=account_breakdown,
        support_plan=support_plan,
    )


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
    "--services", "-s", default=None,
    help="Comma-separated list of services to update. Defaults to all of them: "
         "ECS, Lambda, EC2, EBS, RDS, ElastiCache, S3, SQS, CloudWatch, ELB, SecretsManager.",
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
    svc_list = (
        [s.strip() for s in services.split(",") if s.strip()] if services else ALL_SERVICES
    )
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


@cli.command("price-state")
@click.option(
    "--input", "-i", "input_path", default="-", show_default=True,
    help="Path to `terraform show -json` output, or - for stdin",
)
@click.option("--output", "-o", "output_path", default="report.html", show_default=True, help="Output HTML path")
@click.option("--region", "-r", default="us-east-1", show_default=True, help="AWS region for price lookups")
@click.option("--json-output", "json_output_path", default=None, help="Also write the priced Infracost-style JSON here")
@click.option("--no-diff", is_flag=True, help="Report the plan's total only, skipping the cost delta.")
@_support_plan_option
def price_state(input_path, output_path, region, json_output_path, no_diff, support_plan):
    """Price a terraform plan/state directly against the local price cache.

    No Infracost API key required. Feed it `terraform show -json`:

        terraform show -json tfplan | bucksawz price-state -o report.html

    Given a plan, the report also shows the monthly cost delta the plan would
    cause. A plain state export has nothing to compare against, so it doesn't.
    """
    import sys
    import dataclasses
    from .pricing.tf_state import is_plan, parse_prior, parse_state
    from .pricing.pricer import build_output, price_resources

    text = sys.stdin.read() if input_path == "-" else open(input_path).read()
    data = json.loads(text)
    priced = price_resources(parse_state(data), region)

    priced_prior = None
    if is_plan(data) and not no_diff:
        priced_prior = price_resources(parse_prior(data), region)

    output = build_output(priced, region, prior_resources=priced_prior)

    if json_output_path:
        with open(json_output_path, "w") as f:
            json.dump(dataclasses.asdict(output), f, indent=2, default=str)
        click.echo(f"JSON written to {json_output_path}")

    render(output, output_path, support_plan=support_plan)
    click.echo(f"Priced {len(priced)} resource(s) from terraform state -> {output_path}")
    diff = output.projects[0].diff
    if diff is not None:
        delta = diff.total_monthly_cost or 0.0
        line = (
            f"Plan changes {len(diff.resources)} resource(s): "
            f"{'+' if delta >= 0 else '-'}${abs(delta):,.2f}/mo"
        )
        if support_plan:
            from .pricing.support import support_delta
            past = output.projects[0].past_breakdown
            past_total = (past.total_monthly_cost or 0.0) if past else 0.0
            with_support = delta + support_delta(past_total, past_total + delta, support_plan)
            line += f" ({'+' if with_support >= 0 else '-'}${abs(with_support):,.2f} with support)"
        click.echo(line)


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
