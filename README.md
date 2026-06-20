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

## Typical workflow

```bash
# 1. Generate infracost JSON (requires Infracost CLI + INFRACOST_API_KEY)
infracost breakdown --path . --format json --out-file infracost.json

# 2. Enrich with real AWS billing data (Cost Explorer + CloudWatch)
#    Pulls 90 days of actuals, estimates usage-based costs, caches for 7 days.
#    Run from your management/payer account to get a per-member-account breakdown.
bucksawz enrich \
  --input infracost.json \
  --output enriched.json \
  --aws-profile my-profile \
  --lookback-days 90

# 3. Generate the interactive HTML report
bucksawz report --input enriched.json --output report.html
open report.html
```

`enrich` is optional — `bucksawz report --input infracost.json` works without AWS
credentials and produces the same HTML minus the historical actuals and estimates.

### Starting from an existing HTML report (no JSON)

```bash
# Re-render a better report from an old infracost HTML export
bucksawz from-html infracost_report.html --output report.html

# Or extract the JSON first for further processing
bucksawz html-to-json infracost_report.html --output infracost.json
```

## Usage reference

### `report` — generate HTML from infracost JSON

```bash
bucksawz report --input infracost.json --output report.html
# Also accepts enriched.json; cost estimates are shown automatically if present
```

### `enrich` — pull AWS Cost Explorer + CloudWatch actuals

```bash
bucksawz enrich \
  --input infracost.json \
  --output enriched.json \
  --aws-profile my-profile \
  --aws-region us-east-1 \   # Cost Explorer region (always us-east-1 for global CE)
  --lookback-days 90 \
  --cache-ttl 7 \
  --force-refresh \           # bypass cache for this run
  --no-cloudwatch             # skip CloudWatch metric enrichment
```

When run from an AWS Organizations management account, per-member-account cost
breakdowns appear automatically in the report — no extra flags needed.

### `prices` — local AWS Pricing API cache

```bash
# Pre-fetch prices for key services (requires pricing:GetProducts)
bucksawz prices update \
  --services ECS,Lambda,EC2,RDS \
  --regions us-east-1,eu-west-2,ap-southeast-2 \
  --aws-profile my-profile

bucksawz prices info   # show DB location and row counts
```

### Cache management

```bash
bucksawz cache info        # show Cost Explorer / CloudWatch cache entries and age
bucksawz cache clear       # remove expired entries
bucksawz cache clear --all # wipe everything
```

## AWS permissions required

### `enrich` (Cost Explorer + CloudWatch)

```json
{
  "Effect": "Allow",
  "Action": [
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "cloudwatch:GetMetricStatistics",
    "elasticloadbalancing:DescribeLoadBalancers"
  ],
  "Resource": "*"
}
```

`elasticloadbalancing:DescribeLoadBalancers` is needed to resolve ALB/NLB CloudWatch
dimension values (the ARN suffix, not the name). It is only called when ALB/NLB
resources are present in the infracost JSON. Use `--no-cloudwatch` to skip it.

For per-account breakdowns, run `enrich` with credentials that have
`ce:GetCostAndUsage` in the management/payer account. No extra permissions needed —
the LINKED_ACCOUNT dimension is returned automatically.

### `prices update` (AWS Pricing API)

```json
{
  "Effect": "Allow",
  "Action": [
    "pricing:GetProducts"
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

- [x] Rich HTML report with sidebar ToC, charts, collapsible sections, search
- [x] CloudWatch enrichment for usage-based costs (ALB LCU, Lambda, SQS, API Gateway)
- [x] AWS Pricing API price cache (`bucksawz prices update`) for ECS/Fargate, Lambda, EC2, RDS
- [x] Usage-based cost estimation: CloudWatch actuals × unit price → `~$X.XX/mo`
- [x] Per-account breakdown for AWS Organizations / consolidated billing
- [x] GitHub Actions workflow (PR comment with cost summary + artifact link)
- [ ] Standalone pricing engine: estimate costs directly from Terraform plan JSON without Infracost
- [ ] ELB/ALB/NLB pricing in the price cache (currently only in the estimator via infracost unit prices)
- [ ] Multi-region enrichment (currently one CE region per `enrich` run)
- [ ] Account alias resolution (show account names alongside IDs)

## License

Apache 2.0 — see [LICENSE](LICENSE).
