# bucksawz

A liberally-licensed (Apache 2.0) cloud cost reporting tool compatible with [Infracost](https://github.com/infracost/infracost) JSON output. Produces richer HTML reports and can enrich estimates with real AWS billing data from Cost Explorer.

## Why

Infracost's HTML report is a single flat scroll — no navigation, no charts, no interactivity — that becomes unusable at scale (a typical multi-stack report is 50k–80k lines). bucksawz replaces the report layer and adds AWS historical cost enrichment to fill in usage-based estimates with actual data.

## Features

- **Rich HTML reports** from Infracost JSON *or* directly from existing Infracost HTML reports
  - Sidebar table of contents with per-project costs
  - Cost-by-project bar chart and cost-by-service donut chart (Chart.js, vendored — no CDN)
  - Top 10 most expensive resources
  - Usage-based cost summary (items that "depend on usage")
  - Collapsible per-project breakdown sections
  - JS search/filter by resource name or project
  - Print-friendly CSS with per-project page breaks
- **AWS Cost Explorer enrichment** — pull 90 days of actual billing data and merge it with estimates
  - Fills in usage-based cost estimates with real p50 actuals
  - 30-day cost forecast
  - 7-day local disk cache so repeated runs don't re-hit the AWS API
- **HTML → JSON converter** — reconstruct an Infracost JSON schema from an existing HTML report

## Installation

```bash
git clone https://github.com/yourusername/bucksawz
cd bucksawz
uv venv .venv && source .venv/bin/activate
uv pip install -e .
```

Requires Python 3.11+. AWS enrichment requires `boto3` (included) and valid AWS credentials.

## Usage

### Generate a report from Infracost JSON

```bash
# Run infracost as usual
infracost breakdown --path . --format json --out-file infracost.json

# Generate the rich HTML report
bucksawz report --input infracost.json --output report.html
```

### Generate a report from an existing Infracost HTML report

If you only have the HTML output (not the original JSON):

```bash
bucksawz from-html infracost_report.html --output report.html
```

### Enrich with AWS Cost Explorer actuals

```bash
bucksawz enrich \
  --input infracost.json \
  --output enriched.json \
  --aws-profile my-profile \
  --lookback-days 90

# Then generate the report from enriched data
bucksawz report --input enriched.json --output report.html
```

Results are cached in `~/.cache/bucksawz/` for 7 days. Override with:

```bash
--cache-ttl 14          # extend to 14 days
--force-refresh         # bypass cache for this run
```

### Cache management

```bash
bucksawz cache info        # show cached entries and their age
bucksawz cache clear       # remove expired entries
bucksawz cache clear --all # wipe everything
```

### Convert HTML report to JSON

```bash
bucksawz html-to-json infracost_report.html --output infracost.json
```

## AWS permissions required

The `enrich` command uses read-only Cost Explorer APIs:

```json
{
  "Effect": "Allow",
  "Action": [
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "ce:GetReservationUtilization",
    "ce:GetSavingsPlansUtilization"
  ],
  "Resource": "*"
}
```

## Relationship to Infracost

bucksawz is not a fork of Infracost. It is a separate tool that:

- Consumes the [Infracost JSON output schema](https://github.com/infracost/infracost) as its primary input format
- Does not call `cloud.infracost.io` or any proprietary pricing API
- Is licensed under Apache 2.0

Infracost itself is also Apache 2.0. bucksawz aims to be a drop-in replacement for the report layer, and eventually (Phase 3) for the pricing engine via the public AWS Pricing API.

## Roadmap

- [ ] Phase 2 complete: CloudWatch metric enrichment for usage-based resources (ALB LCU, Lambda invocations, SQS messages)
- [ ] Phase 3: Replace `cloud.infracost.io` with direct AWS Pricing API — starting with EC2, ELB, RDS, ECS, S3
- [ ] Multi-account Cost Explorer support
- [ ] GitHub Actions integration (post report as PR comment)

## License

Apache 2.0 — see [LICENSE](LICENSE).
