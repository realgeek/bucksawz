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
def enrich(input_path, output_path, lookback_days, aws_profile, aws_region):
    """Enrich infracost JSON with AWS Cost Explorer actuals."""
    from .aws.costexplorer import enrich_output
    import json
    output = InfracostOutput.from_file(input_path)
    enriched = enrich_output(output, lookback_days=lookback_days, profile=aws_profile, region=aws_region)
    with open(output_path, "w") as f:
        json.dump(enriched, f, indent=2)
    click.echo(f"Enriched output written to {output_path}")


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
