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
- **Standalone pricing** — price a `terraform show -json` plan straight from a local AWS
  Pricing API cache, no Infracost CLI or API key involved (`bucksawz price-state`)
- **Plan cost diffs** — what a plan does to the monthly bill, per resource
  (added / removed / changed), from either a Terraform plan or an Infracost diff
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

### Without Infracost

```bash
# 1. Populate the local price cache (needs pricing:GetProducts)
bucksawz prices update --regions us-east-1 --aws-profile my-profile

# 2. Price a plan directly
terraform show -json tfplan | bucksawz price-state --region us-east-1 -o report.html
```

Coverage is narrower than Infracost's — see [`price-state`](#price-state--price-a-terraform-plan-with-no-infracost)
for the supported resource types.

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
# Pre-fetch prices (requires pricing:GetProducts). Defaults to every supported
# service: ECS, Lambda, EC2, EBS, RDS, ElastiCache, S3, SQS, CloudWatch, ELB,
# SecretsManager, Route53, KMS, WAF.
bucksawz prices update \
  --regions us-east-1,eu-west-2,ap-southeast-2 \
  --aws-profile my-profile

# Or narrow it down
bucksawz prices update --services EC2,RDS --regions us-east-1

bucksawz prices info   # show DB location and row counts
```

### `price-state` — price a Terraform plan with no Infracost

```bash
terraform show -json tfplan > plan.json
bucksawz price-state --input plan.json --output report.html --region us-east-1

# Or straight off a pipe, keeping the intermediate JSON
terraform show -json tfplan | bucksawz price-state -o report.html --json-output priced.json
```

Given a **plan** (rather than a plain state export), the report also shows what the
plan would do to your monthly bill — a total delta plus a per-resource
added/removed/changed table. Pass `--no-diff` to report the absolute total only.

```bash
$ terraform show -json tfplan | bucksawz price-state -o report.html
Priced 4 resource(s) from terraform state -> report.html
Plan changes 4 resource(s): +$42.05/mo
```

The same section appears for Infracost input that carries a diff (`infracost diff
--format json`), since bucksawz reads the standard `pastBreakdown` and `diff` fields.

Prices come from the local cache, so run `bucksawz prices update` first. Resource
types priced today:

| Terraform type | Cost basis |
| --- | --- |
| `aws_instance`, `aws_launch_template` | on-demand instance hours (Linux, shared tenancy) |
| `aws_ebs_volume`, plus `root_block_device`/`ebs_block_device`/`block_device_mappings` on the above | per-GB-month storage (7 volume types) + provisioned IOPS/throughput above the gp3 baseline, io1/io2 tiering |
| `aws_db_instance`, `aws_rds_cluster_instance` | instance hours by engine + Single/Multi-AZ |
| `aws_elasticache_cluster`, `aws_elasticache_replication_group` | node hours × node count |
| `aws_ecs_task_definition` (Fargate) | vCPU + GB hours, x86 or ARM |
| `aws_lb`, `aws_alb`, `aws_elb` | load balancer hours, plus LCU/data-processed unit price |
| `aws_lambda_function` | request + GB-second unit prices (usage-based) |
| `aws_s3_bucket` | Standard storage GB-month unit price (usage-based) |
| `aws_sqs_queue` | request unit price, Standard or FIFO (usage-based) |
| `aws_secretsmanager_secret` | flat $0.40/mo per secret, plus API request unit price (usage-based) |
| `aws_route53_zone` | flat hosted zone rate (first-tier), plus standard query unit price (usage-based) |
| `aws_kms_key` | flat $1/mo per customer-managed key, plus symmetric API request unit price (usage-based) |
| `aws_wafv2_web_acl` | flat web ACL rate + $1/mo per `rule` block, plus baseline request unit price (usage-based) |

EBS volumes attached to an instance or launch template are priced as sub-resources
of it and folded into its total; a standalone `aws_ebs_volume` prices the same way
on its own. gp3's first 3,000 IOPS / 125 MiB/s are included in the storage price —
only usage above that is billed separately; io1 has no free IOPS tier; io2 IOPS is
billed across three AWS-fixed tiers.

Unlike the other usage-based resources, a Secrets Manager secret, Route 53 hosted
zone, KMS key, and WAF web ACL/rule all have a base price that's fixed and known
from Terraform config alone — so `monthly_cost` is populated for those even though
their request/query volume components stay usage-based. Route 53 pricing uses only
the first tier (first 25 zones, first 1B queries/mo); WAF's request price uses the
flat baseline rate rather than modelling its Web ACL Capacity Unit (WCU) tiers,
which depend on rule complexity bucksawz can't compute from Terraform config alone.

Usage-based rows carry a unit price but no monthly total — quantity isn't knowable
from a Terraform config. Anything unrecognised is listed as no-price rather than
dropped, so the report still accounts for it. Everything else is out of scope for
now: NAT gateways, data transfer, and reserved/savings-plan discounts are not
modelled.

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
- [x] AWS Pricing API price cache (`bucksawz prices update`) for ECS/Fargate, Lambda, EC2, RDS, ElastiCache, S3, SQS, CloudWatch, ELB
- [x] Usage-based cost estimation: CloudWatch actuals × unit price → `~$X.XX/mo`
- [x] Per-account breakdown for AWS Organizations / consolidated billing
- [x] GitHub Actions workflow (PR comment with cost summary + artifact link)
- [x] Standalone pricing engine: estimate costs directly from Terraform plan JSON without Infracost (`bucksawz price-state`)
- [x] ELB/ALB/NLB pricing in the price cache
- [x] Plan cost diffs — per-resource delta from a Terraform plan or an Infracost `diff`
- [x] EBS pricing: storage (7 volume types), provisioned IOPS/throughput, root/attached volumes
- [x] Secrets Manager pricing: flat per-secret rate + usage-based API requests
- [x] Route 53, KMS, and WAFv2 pricing: flat base rates (zone/key/ACL+rules) + usage-based request/query components
- [ ] Broaden `price-state` coverage further: NAT gateways, data transfer, Config
- [ ] CloudWatch metrics for S3 bucket size and CloudWatch Logs volume, so those unit prices resolve to real estimates
- [ ] Multi-region `price-state` (currently one `--region` per run; a plan spanning providers is priced against one region)
- [ ] Multi-region enrichment (currently one CE region per `enrich` run)
- [ ] Account alias resolution (show account names alongside IDs)
- [ ] Put the plan delta in the GitHub Actions PR comment (the workflow still runs
      `infracost breakdown`, which has no prior state to compare against)

## License

Apache 2.0 — see [LICENSE](LICENSE).
