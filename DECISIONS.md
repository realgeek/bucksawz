# bucksawz — decision log

Decisions made during the initial implementation session (2026-06-20).

---

## Licensing

**Decision:** Build as an independent Apache 2.0 tool, not a fork of Infracost source.

**Rationale:** Infracost CLI is Apache 2.0 (confirmed on master/all versions through 2.0 — no BUSL change occurred). However, its pricing data comes from a proprietary cloud backend (`cloud.infracost.io`) that is not part of the open-source repo. Rather than forking the Go source and immediately needing to replace the pricing backend, we treat infracost as a *data source* (JSON output) and build bucksawz as an independent layer on top. This delivers value immediately without needing to reimplement 1,100+ resource definitions on day one.

**Consequence:** Phase 3 (pricing engine) will need to call the AWS Pricing API directly rather than inheriting infracost's pricing logic.

---

## Language: Python, not Go

**Decision:** Python 3.11+, managed via `uv`.

**Rationale:** Go was not installed in the environment. Python 3.13, Node 20, and Ruby were available. Python was chosen for its strong HTML/JSON tooling, boto3 for AWS APIs, Jinja2 for templating, and Click for the CLI. No framework overhead — stdlib + three dependencies.

---

## Input format: infracost JSON schema

**Decision:** Primary input is `infracost breakdown --format json` output. Secondary input is existing infracost HTML reports (via `from-html` / `html-to-json` commands).

**Rationale:** The JSON schema is well-defined and versioned. The HTML parser was added specifically because the sample file provided was an HTML report with no accompanying JSON — it also makes bucksawz useful for re-processing historical reports.

**Schema types:** `InfracostOutput → Project → Breakdown → Resource → CostComponent` (dataclasses in `bucksawz/schema/infracost.py`).

---

## HTML report: self-contained single file

**Decision:** Output is a single `.html` file with all CSS, JS, and chart library inlined. No CDN dependencies at render time.

**Rationale:** Reports are artifacts — shared in PRs, attached to CI runs, stored in S3. External CDN references break in air-gapped environments, cause CSP issues, and rot when CDN URLs change.

**Implementation:** Chart.js 4.4.4 downloaded at build time and stored in `bucksawz/report/assets/chart.min.js` (205KB). Inlined into the template via Jinja2 `{{ chartjs | safe }}`.

---

## Chart library: Chart.js (vendored)

**Decision:** Chart.js 4.4.4, vendored locally.

**Rationale:** Widely used, well-documented, compact minified size (~200KB), supports horizontal bar charts and donut charts out of the box. No build step required — pure JS loaded via a `<script>` tag. Alternatives considered: D3 (too large, steep learning curve for simple charts), Plotly (much larger), ApexCharts (less common).

---

## Sidebar layout with sticky ToC

**Decision:** Two-column layout: fixed 260px dark sidebar (ToC + nav links) + scrollable main content area.

**Rationale:** The original infracost HTML has no navigation. With 21 projects and a 76K-line flat file, the report was effectively unusable in a browser. A sticky sidebar ToC with per-project costs and active-link highlighting on scroll makes the report navigable at any scale.

**Print behaviour:** Sidebar hidden via `@media print`; layout collapses to full-width for PDF export.

---

## Collapsible project sections via `<details>`

**Decision:** Each project section uses native HTML `<details>`/`<summary>` elements. First 3 projects open by default.

**Rationale:** No JS required for basic collapse/expand. Reduces initial render weight for large reports. Native browser behaviour means print/PDF export can be controlled via CSS (`details { display: block }`). The first 3 are open to give immediate context without requiring user interaction.

---

## Service categorisation from resource type/name

**Decision:** `Resource.aws_service()` infers AWS service category from `resource_type` first, falling back to the resource name prefix (e.g. `aws_lb.main` → `aws_lb` → `"ELB"`).

**Rationale:** When parsing from HTML, `resourceType` is not a separate field — it's embedded in the resource name. The fallback covers the HTML-parsed path. When reading from JSON, `resourceType` is explicit and takes precedence.

---

## Local disk cache for AWS API results

**Decision:** `~/.cache/bucksawz/` — JSON envelope files keyed by SHA-256 of (operation, profile, region, date-range). Default TTL: 7 days.

**Rationale:** Cost Explorer and CloudWatch calls are slow (~2–5s each) and billed per API call. Re-running `enrich` for report iteration shouldn't re-fetch. 7 days chosen as a balance between freshness and cost: billing data doesn't change retroactively, and a week is short enough that estimates remain relevant.

**Override mechanisms:**
- `$BUCKSAWZ_CACHE_DIR` env var to relocate the cache
- `--cache-ttl N` to change TTL per run
- `--force-refresh` to bypass cache entirely
- `bucksawz cache clear` / `cache clear --all` for manual cleanup

---

## Cost Explorer enrichment strategy

**Decision:** `GetCostAndUsage` grouped by `SERVICE` dimension, not by resource tag or ARN.

**Rationale:** Resource-level Cost Explorer data requires Cost Allocation Tags to be enabled and propagated — not a safe assumption. Service-level data is always available. The service total is attached to all resources of that service type, which is approximate but always works.

**Future improvement:** If `aws:createdBy` or custom tags are present, resource-level matching could be added as an opt-in.

---

## CloudWatch enrichment strategy

**Decision:** Best-effort match by Terraform resource name suffix → AWS resource name. ALBs resolved via `elbv2:DescribeLoadBalancers` to get the CloudWatch dimension value (ARN suffix, not name).

**Rationale:** CloudWatch metrics for ALBs use the LoadBalancer ARN suffix as the dimension value, not the name. A pre-fetch of all load balancers builds a name→ARN-suffix map. SQS and Lambda use the queue/function name directly, which typically matches the Terraform resource name suffix.

**Fallback:** If the name lookup fails, the resource is silently skipped (no error). CloudWatch enrichment is best-effort — partial data is valid.

**Escape hatch:** `--no-cloudwatch` flag skips all CloudWatch calls (Cost Explorer only).

---

## GitHub Actions workflow design

**Decision:** Trigger on PRs touching `.tf`/`.hcl` files. OIDC for AWS auth (not static credentials). Cost Explorer enrichment is optional — workflow proceeds without it if the IAM role isn't configured.

**Rationale:**
- OIDC avoids storing long-lived AWS credentials as secrets
- `continue-on-error: true` on the aws-auth step + conditional on `steps.aws-auth.outcome == 'success'` means the workflow degrades gracefully for repos that haven't set up the IAM role yet
- PR comment is idempotent — finds and updates existing bucksawz comment rather than creating duplicates on each push

**Comment marker:** `<!-- bucksawz-cost-report -->` as an invisible HTML comment to identify the managed comment for updates.

---

## Test strategy

**Decision:** Three test modules covering schema parsing, HTML parser, and cache. One integration-style test module for the full report render pipeline.

- `test_schema.py` — JSON schema types, cost rollup, service categorisation, usage-based flag detection
- `test_html_parser.py` — HTML→schema parsing using a minimal in-memory HTML fixture (no file I/O)
- `test_cache.py` — TTL expiry, invalidation, type serialisation; cache directory isolated per test via `monkeypatch`
- `test_report.py` — end-to-end JSON→HTML render using `tests/fixtures/infracost_minimal.json`; 18 assertions on the rendered HTML

**No mocking of AWS clients** — enrichment tests are not included (would require mocking boto3). Those are integration-tested manually or in a future `tests/integration/` directory with real credentials.

---

## What's next (Phase 3)

**Pricing engine** — replace `cloud.infracost.io` dependency with direct AWS Pricing API calls. Priority order by spend share in the sample report:

1. ECS (Fargate) — `AWS/ECS`, `pricing:GetProducts` for `AmazonECS`
2. RDS — `AmazonRDS`
3. ELB — `AmazonEC2` (ELB is under EC2 pricing namespace)
4. EC2 — `AmazonEC2`
5. S3, Lambda, SQS, Route 53, CloudWatch, NAT Gateway, ECR, SecretsManager, CodeBuild

Local SQLite cache for pricing data, refreshed via `bucksawz update-prices`. This is the largest remaining work item — compact context before starting.
