# bucksawz — plan

A liberally-licensed (Apache 2.0) cloud cost reporting and estimation tool, compatible with the Infracost JSON output schema and improving on it.

## Licensing situation

Infracost CLI (all versions through 2.0) is Apache 2.0. No BUSL/license change occurred. The proprietary piece is the pricing backend (`cloud.infracost.io`) — not part of the open-source repo. We can fork the CLI source and replace that backend.

## Architecture

Three phases, each independently useful:

### Phase 1 — Rich HTML report generator (immediate)

`bucksawz report --input infracost.json --output report.html`

Reads the published infracost JSON schema. Outputs a single-file HTML report with:

- **Header**: total cost, resource count cards, generated-at timestamp
- **Table of Contents**: anchor-linked list of all projects with $ cost next to each name
- **Executive summary**:
  - Horizontal bar chart: cost by project (Chart.js, inlined)
  - Donut chart: cost by AWS service category
  - Top 10 most expensive resources table
  - "Cost depends on usage" aggregated list
- **Per-project sections**: collapsible `<details>` blocks, each with breakdown table
- **JS search/filter**: filter by resource name, project, or tag
- **Print CSS**: `@media print` with page breaks per project
- **Warnings footer**: unsupported resource types

Implementation: Go binary, Chart.js vendored and base64-inlined, zero external deps at runtime.

### Phase 2 — AWS historical cost enrichment

`bucksawz enrich --input infracost.json --output enriched.json --lookback-days 90`

- Pulls `ce:GetCostAndUsage` for lookback window grouped by service + resource tag
- Pulls CloudWatch metrics for usage-based resources (ALB LCU, SQS messages, Lambda invocations)
- Fills "cost depends on usage" quantities with p50 actual from the lookback period
- Adds `historical` block to each resource in the output JSON
- Adds `ce:GetCostForecast` 30-day projection
- Shows RI/SP utilisation from `ce:GetReservationUtilization`

IAM: `ce:Get*`, `cloudwatch:GetMetricStatistics` (read-only).

### Phase 3 — Replace proprietary pricing backend (longer term)

Fork infracost Go source, strip cloud.infracost.io calls, replace with:
- AWS Pricing API (`pricing:GetProducts`)
- AWS bulk pricing JSON (public, no auth)
- Local SQLite cache refreshed via `bucksawz update-prices`

Start with 20 most common resource types (EC2, ELB, RDS, S3, Lambda, CloudFront, Route53, ECS, ECR, SQS, SNS, CloudWatch, VPC, NAT GW, EBS, ElastiCache, CodeBuild, SecretsManager, ACM, IAM) covering ~90% of typical bills.

## Project structure

```
bucksawz/
├── cmd/
│   ├── main.go
│   ├── report.go       # Phase 1
│   ├── enrich.go       # Phase 2
│   └── prices.go       # Phase 3
├── internal/
│   ├── schema/
│   │   └── infracost.go    # JSON schema types
│   ├── report/
│   │   ├── render.go       # HTML generation
│   │   ├── template.go     # embedded HTML/CSS/JS template
│   │   └── assets/
│   │       ├── chart.min.js    # Chart.js vendored
│   │       └── style.css
│   ├── aws/
│   │   ├── costexplorer/
│   │   └── cloudwatch/
│   └── reconcile/
├── go.mod
├── go.sum
├── LICENSE
└── PLAN.md
```

## Implementation order

1. Go module scaffold + schema types (from infracost JSON schema)
2. `report` command — HTML template + chart rendering
3. Test against `infracost_20260513_110742.html` source data (need the JSON; can parse HTML as fallback)
4. `enrich` command — Cost Explorer integration
5. Phase 3 pricing engine

## Token budget note

Agreed to stay within 65K tokens from the point this file was written. Implementation proceeds non-interactively.
