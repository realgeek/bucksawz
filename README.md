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

# Resources living in more than one region: CloudWatch metrics are regional
# (unlike Cost Explorer's account-wide cost totals), so search every region a
# usage-based resource might be in. Each resource is tried against each listed
# region in order until one returns datapoints.
bucksawz enrich \
  --input infracost.json --output enriched.json \
  --cloudwatch-regions us-east-1,eu-west-1,ap-southeast-2
```

When run from an AWS Organizations management account, per-member-account cost
breakdowns appear automatically in the report — no extra flags needed, and
account names resolved via Organizations show alongside the IDs.

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

Some costs (data transfer today) depend on account-wide usage a single Terraform
plan can't express. `price-state` still surfaces their unit prices, and
`--usage-file` turns a known monthly quantity into a real estimate:

```bash
cat > usage.yml <<EOF
data_transfer:
  internet_egress_gb_month: 10000
  inter_az_gb_month: 500
EOF
bucksawz price-state --input plan.json -o report.html --usage-file usage.yml
```

The estimate shows up in the report's "usage-based costs" table, same as a
CloudWatch-actuals estimate from `enrich` — indicative, not a substitute for
billed cost.

If you have AWS account access, `--aws-profile` pulls real data-transfer volume
from Cost Explorer instead of a static number, superseding `--usage-file`'s
`data_transfer` values when it finds matching usage:

```bash
bucksawz price-state --input plan.json -o report.html --aws-profile prod --ce-lookback-days 30
```

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
| `aws_nat_gateway` | flat hourly rate, plus per-GB data-processed unit price (usage-based) |
| `aws_config_configuration_recorder` | configuration-item-recorded unit price (usage-based) |
| `aws_config_config_rule` | rule-evaluation unit price (usage-based) |
| `aws_cloudwatch_metric_alarm` | flat $0.10/mo per alarm |
| `aws_cloudwatch_log_group` | data-ingested + data-stored unit prices (both usage-based) |

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
- [x] Data transfer pricing: every internet-egress tier + flat inter-AZ rate, as an informational unit-priced resource (real quantities need a usage file or CUR actuals — not yet implemented)
- [x] NAT gateway pricing: flat hourly rate + usage-based per-GB data-processed rate
- [x] AWS Config pricing: fully usage-based configuration-item and rule-evaluation unit prices (real totals need account usage data, same as data transfer — see below)
- [x] `--usage-file` for data transfer: user-supplied monthly GB (Infracost-usage-file style) turned into a real estimate, split across every egress tier
- [x] Data transfer usage sourcing, layer 3: Cost Explorer actuals (`--aws-profile`) supersede `--usage-file` when the account has matching data-transfer spend
- [x] CloudWatch metrics for S3 bucket size and CloudWatch Logs volume, so those unit prices resolve to real estimates via `enrich`
- [x] Price CloudWatch alarms and log groups directly in `price-state` (previously fetched into the price cache but never consumed by any pricer)
- [x] Multi-region `price-state`: each resource prices against its own provider's region (including aliased providers passed into child modules) when the plan resolves one to a literal string; `--region` is now only the fallback for resources whose region isn't statically resolvable. Data transfer (a synthetic, non-resource cost) and Cost Explorer actuals still use a single `--region`/`--aws-profile` pair per run.
- [x] Multi-region enrichment: `enrich --cloudwatch-regions us-east-1,eu-west-1,...` searches every listed region for each usage-based resource's CloudWatch metrics, since Cost Explorer's cost totals are already account-wide (not filtered by region) and only CloudWatch actuals were single-region. The Infracost JSON schema carries no per-resource region, so a resource is tried against each region in order until one returns datapoints — see `enrich_with_cloudwatch`'s docstring for the name-collision caveat this implies.
- [x] Account alias resolution: `enrich` resolves AWS Organizations account names via `list_accounts` (management/delegated-admin accounts only) and the report shows them alongside account IDs in the per-account breakdown
- [x] Put the plan delta in the GitHub Actions PR comment: the workflow now checks out the PR base ref into a worktree, runs `infracost breakdown` there, and feeds it to `infracost diff --compare-to` so the JSON carries a real per-resource delta (which `bucksawz report`'s existing "Plan changes" section already rendered) — the PR comment itself now also shows a one-line `Change vs. base branch: ±$X.XX/mo` summary alongside the artifact link. Falls back to a plain `infracost breakdown` (no delta) if the base checkout/breakdown fails.
- [x] EKS pricing in `price-state`: flat control-plane hourly rate (`aws_eks_cluster`) — worker capacity is priced separately by the existing EC2/Fargate pricers.
- [x] DynamoDB pricing in `price-state`: storage is always usage-based; provisioned tables (`billing_mode = "PROVISIONED"`) get a known monthly cost from `read_capacity`/`write_capacity`, on-demand tables (`PAY_PER_REQUEST`) leave request-unit volume usage-based, same shape as S3/SQS.
- [x] VPC Interface Endpoint pricing: flat hourly rate per AZ (from `subnet_ids`) + usage-based per-GB data processed; Gateway endpoints (S3/DynamoDB) price as a known $0.
- [x] SNS pricing: fully usage-based per-request rate, same shape as SQS.
- [x] EFS pricing: fully usage-based Standard-class storage rate, same shape as S3.
- [x] ECR pricing: fully usage-based private-repository storage rate, same shape as S3/EFS.
- [x] API Gateway pricing: fully usage-based per-request rate for REST APIs (`aws_api_gateway_rest_api`) and HTTP APIs (`aws_apigatewayv2_api`); WebSocket APIs (message/connection-minute billing) come back unsupported rather than priced at the wrong rate. Written without live Pricing API access — see `fetch_apigateway`'s docstring caveat.
- [x] CloudFront pricing: fully usage-based data-transfer-out and HTTPS-request rates for `aws_cloudfront_distribution`, priced against only the cheapest US/Canada/Europe edge-location group and its first volume tier (a coarse simplification — see `fetch_cloudfront`'s docstring). Stored under whatever region key is requested, like Route 53, since CloudFront pricing isn't AWS-region-scoped. Written without live Pricing API access — verify before trusting.
- [x] Kinesis Data Streams pricing: provisioned mode (terraform's default) gets a known monthly cost from `shard_count`, same shape as EC2 instance-hours, plus usage-based PUT payload units; on-demand mode (`stream_mode_details.stream_mode = "ON_DEMAND"`) bills per-GB instead of per-shard and comes back unsupported rather than guessing. Written without live Pricing API access — verify before trusting.
- [x] Step Functions pricing: fully usage-based state-transition rate for Standard workflows, and request + GB-second duration rates for Express workflows (`aws_sfn_state_machine.type`). Written without live Pricing API access — verify before trusting.
- [x] EventBridge pricing: fully usage-based per-custom-event rate for `aws_cloudwatch_event_bus`. Written without live Pricing API access — verify before trusting.
- [x] Transit Gateway pricing: flat per-attachment hourly rate + usage-based per-GB data processed for `aws_ec2_transit_gateway_vpc_attachment`, same hourly-plus-usage shape as NAT Gateway/VPC Interface Endpoints.
- [x] S3 Files pricing (`aws_s3files_file_system`): usage-based cache storage + GET/PUT request rates — the underlying bucket's own S3 storage cost is priced separately by the existing `aws_s3_bucket` pricer, not duplicated here. Mount targets and access points (`aws_s3files_mount_target`, `aws_s3files_access_point`) price as a known $0, matching AWS's published free tier for those. This is a brand-new AWS service (launched after this codebase's knowledge cutoff, no live Pricing API access to verify against) — the service code and usagetype filters in `fetch_s3files` are a best-effort guess and need checking against a real `bucksawz prices update --services S3Files` run before trusting. `aws_s3files_synchronization_configuration` and `aws_s3files_file_system_policy` aren't independently billed and are intentionally left unregistered.
- [x] OpenSearch/Elasticsearch Service pricing (`aws_opensearch_domain`, aliased for `aws_elasticsearch_domain`): config-derivable data-node instance-hours (`cluster_config.instance_type`/`instance_count`) plus EBS storage (`ebs_options.volume_size`/`volume_type`, defaulting to gp2 like AWS itself does when unset). Dedicated master nodes and UltraWarm/cold-storage nodes are not priced — a domain using either will under-report its real cost. Written without live Pricing API access — the `AmazonES` product family names and gp2/gp3 volume-type filters in `fetch_opensearch` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services OpenSearch` and sanity-check `bucksawz prices info` before trusting it.
- [x] Redshift pricing (`aws_redshift_cluster`): config-derivable compute node-hours (`node_type`/`number_of_nodes`), plus a usage-based managed-storage component for RA3 node types only — DC2/DS2 bundle storage into the node-hour rate, so they get no separate storage line. Reserved-instance pricing and Redshift Serverless (a distinct RPU-hour billing model, not a `aws_redshift_cluster`) aren't priced.
- [x] AWS Backup pricing (`aws_backup_vault`): fully usage-based warm/cold storage and restore per-GB rates, like S3/EFS — how much data a vault ends up holding and how much gets restored isn't derivable from Terraform config, it depends on what backup plans and jobs write into it over time. `aws_backup_plan` prices as a known $0 — it's scheduling/policy config, not its own billable resource; the cost it drives lands on the vault. Written without live Pricing API access — the "Warm"/"Cold"/"Restore" usagetype substrings in `fetch_backup` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services Backup` before trusting it.
- [x] MSK (Managed Streaming for Kafka) pricing (`aws_msk_cluster`): config-derivable broker instance-hours (`broker_node_group_info.instance_type`/`number_of_broker_nodes`) plus EBS broker storage (`storage_info.ebs_storage_info.volume_size`) — same shape as RDS+attached-EBS. When `storage_info` is omitted (older provider versions default it), no storage component is added since the default size isn't in this resource's config either. MSK Serverless (a distinct RPU-hour-and-partition billing model, not `aws_msk_cluster`) isn't priced. Written without live Pricing API access — the "Kafka Broker Instance"/"Kafka Broker Storage" product family names in `fetch_msk` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services MSK` before trusting it.
- [x] Elastic IP pricing (`aws_eip`): flat hourly rate, unconditionally. Since AWS's Feb 1, 2024 pricing change, every public IPv4 address costs the same $0.005/hr whether it's attached to a running instance, a stopped one, or nothing at all — the old "free while attached, charged while idle" model is gone, so there's no attachment-state branch to get right (and no need for one, since Terraform config can't tell you an instance's runtime state anyway). Written without live Pricing API access — the "PublicIPv4"/"ElasticIP" usagetype substrings in `fetch_eip` are inferred from AWS's public pricing announcement, not verified against a real `get_products` response; run `bucksawz prices update --services EIP` before trusting it. Only `aws_eip` itself is priced — the free-tier auto-assigned public IP on an `aws_instance` without an EIP, and public IPs implicitly carried by NAT gateways/ALBs/etc., aren't separately modeled here (NAT Gateway's own flat rate already covers its case).
- [x] CloudTrail pricing (`aws_cloudtrail`): fully usage-based management/data/Insights event rates, same pattern as AWS Config — event volume is never in Terraform config, only which categories of events a trail's selectors can incur charges for. Written without live Pricing API access — the "PaidEventsRecorded"/"DataEventsRecorded"/"InsightsEventsRecorded" usagetype substrings in `fetch_cloudtrail` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services CloudTrail` before trusting it.
- [x] GuardDuty pricing (`aws_guardduty_detector`): fully usage-based per-GB analysis rate for the base CloudTrail/DNS-log tier. The separately-billed S3 Protection, EKS Protection, and Malware Protection tiers (enabled via the newer `aws_guardduty_detector_feature` resource) aren't modeled. Written without live Pricing API access — verify with `bucksawz prices update --services GuardDuty` before trusting it.
- [x] DocumentDB pricing (`aws_docdb_cluster_instance`): config-derivable instance-hours by `instance_class`, same shape as RDS. `aws_docdb_cluster`'s own storage/I/O usage is usage-based and has no dedicated pricer.
- [x] FSx for Windows File Server pricing (`aws_fsx_windows_file_system`): config-derivable storage (SSD/HDD by `storage_type`) plus provisioned throughput capacity, both fully known from `storage_capacity`/`throughput_capacity`. FSx for Lustre, ONTAP, and OpenZFS aren't covered yet. Written without live Pricing API access — the "Storage"/"Provisioned Throughput" product families and SSD/HDD `storageMedia` filter in `fetch_fsx_windows` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services FSxWindows` before trusting it.
- [x] ACM Private CA pricing (`aws_acmpca_certificate_authority`): flat monthly per-CA fee, known from config (`usage_mode`: general-purpose or short-lived), plus a usage-based per-certificate-issued component — issuance count isn't in the CA's own config. Public ACM certificates (`aws_acm_certificate`) are free and have no pricer. Written without live Pricing API access — the service code and usagetype substrings in `fetch_acmpca` are inferred from AWS's public pricing page, not verified against a real `get_products` response; run `bucksawz prices update --services ACMPCA` before trusting it.
- [x] Athena pricing (`aws_athena_workgroup`): fully usage-based per-TB-scanned rate, same pattern as Config/CloudTrail — bytes scanned is driven by the query and the data, not by the workgroup's config (`bytes_scanned_cutoff_per_query` caps a query, it doesn't set the volume).
- [x] FSx for Lustre pricing (`aws_fsx_lustre_file_system`): config-derivable storage priced by `deployment_type` + `storage_type`. The within-deployment rate variation driven by `per_unit_storage_throughput` isn't captured — only a single representative rate per deployment/storage-type combination is used. Written without live Pricing API access — verify with `bucksawz prices update --services FSxLustre` before trusting it.
- [x] Neptune pricing (`aws_neptune_cluster_instance`): config-derivable instance-hours by `instance_class`, same shape as RDS/DocumentDB. Cluster storage/I/O (`aws_neptune_cluster`) is usage-based and has no dedicated pricer.
- [x] Global Accelerator pricing (`aws_globalaccelerator_accelerator`): flat fixed hourly fee (global pricing, like Route 53/CloudFront) plus a usage-based data-transfer-premium component. Written without live Pricing API access — verify with `bucksawz prices update --services GlobalAccelerator` before trusting it.
- [x] Amazon MQ pricing (`aws_mq_broker`): config-derivable broker instance-hours by `host_instance_type`, with broker count inferred from `deployment_mode` (single instance vs. active/standby or cluster multi-AZ, both of which double it). Storage is usage-based, and the ActiveMQ-vs-RabbitMQ difference in whether storage is separately billed isn't modeled. Written without live Pricing API access — verify with `bucksawz prices update --services MQ` before trusting it.
- [x] VPN pricing: Site-to-Site VPN (`aws_vpn_connection`) is a flat, config-derivable connection-hour rate. Client VPN (`aws_ec2_client_vpn_endpoint`) is fully usage-based (subnet-association-hours and active-connection-hours) since neither is knowable from the endpoint's own config. Written without live Pricing API access — verify with `bucksawz prices update --services VPN` before trusting it.
- [x] Direct Connect pricing (`aws_dx_connection`, `aws_dx_hosted_connection`): config-derivable port-hour fee by `bandwidth`. Data transferred over the connection is usage-based and isn't modeled. Written without live Pricing API access — verify with `bucksawz prices update --services DirectConnect` before trusting it.
- [x] AppSync pricing (`aws_appsync_graphql_api`): fully usage-based (query/mutation operations, real-time subscription connection-minutes) — request volume isn't derivable from the API's own config. Written without live Pricing API access — verify with `bucksawz prices update --services AppSync` before trusting it.
- [x] Cognito pricing (`aws_cognito_user_pool`): fully usage-based (monthly active users), using a single representative first-tier rate — real MAU pricing is tiered and further split by advanced-security-features, neither resolvable from a single Terraform plan. Written without live Pricing API access — verify with `bucksawz prices update --services Cognito` before trusting it.
- [x] Glue pricing (`aws_glue_job`, `aws_glue_crawler`): fully usage-based (DPU-hours) — run frequency and duration aren't derivable from a job/crawler's own config, even though `max_capacity`/`worker_type`/`number_of_workers` set the per-run DPU rate. Written without live Pricing API access — verify with `bucksawz prices update --services Glue` before trusting it.
- [x] SageMaker pricing: notebook instances (`aws_sagemaker_notebook_instance`) are config-derivable always-on instance-hours by `instance_type`. Endpoint cost lives on `aws_sagemaker_endpoint_configuration` (not `aws_sagemaker_endpoint`, which only references a config by name) — priced per `production_variants` entry by `instance_type` × `initial_instance_count`; serverless variants have no dedicated instance-hour rate and are left unpriced. Written without live Pricing API access — verify with `bucksawz prices update --services SageMaker` before trusting it.
- [x] CloudHSM pricing (`aws_cloudhsm_v2_hsm`): flat HSM-hour rate, no instance-type variation. The cluster resource (`aws_cloudhsm_v2_cluster`) carries no charge of its own — cost is entirely on each HSM. Written without live Pricing API access — verify with `bucksawz prices update --services CloudHSM` before trusting it.
- [x] Macie pricing (`aws_macie2_account`, `aws_macie2_classification_job`): fully usage-based (per-GB data evaluated), using a single representative first-tier rate — real pricing is tiered by cumulative monthly GB, not resolvable from a single Terraform plan. Written without live Pricing API access — verify with `bucksawz prices update --services Macie` before trusting it.
- [x] Inspector pricing (`aws_inspector2_enabler`): fully usage-based, one component per enabled `resource_types` entry (EC2 instance-months, ECR image scans, Lambda function-months) — real instance/image/function counts are account-wide and aren't tied to the enabler's own config. Written without live Pricing API access — verify with `bucksawz prices update --services Inspector` before trusting it.

## License

Apache 2.0 — see [LICENSE](LICENSE).
