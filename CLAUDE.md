# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

bucksawz is an Apache 2.0 cloud cost reporting tool. It consumes [Infracost](https://github.com/infracost/infracost) JSON output (or existing Infracost HTML reports) and produces a richer, self-contained HTML report, optionally enriched with real AWS Cost Explorer / CloudWatch data. It is not a fork of Infracost — it treats Infracost's JSON schema purely as an input format and does not call `cloud.infracost.io`.

Phase 3 (`bucksawz price-state`) can already estimate costs directly from `terraform show -json` via a local AWS Pricing API cache, with no Infracost involved — for a subset of resource types. Broadening that coverage is the ongoing work; see the Roadmap section of README.md and `bucksawz/pricing/`.

## Commands

```bash
# setup
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# tests
pytest                          # full suite
pytest tests/test_pricing.py    # single module
pytest tests/test_report.py::test_name -v   # single test

# CLI (once installed)
bucksawz report --input infracost.json --output report.html
bucksawz enrich --input infracost.json --output enriched.json --aws-profile <profile>
bucksawz prices update --regions us-east-1              # all services by default
bucksawz prices update --services EC2,RDS --regions us-east-1
bucksawz prices info
bucksawz price-state --input plan.json --region us-east-1 -o report.html
bucksawz cache info / cache clear [--all]
bucksawz from-html <report.html>
bucksawz html-to-json <report.html>
```

No linter/formatter config is set up in this repo currently.

## Architecture

Two entry points, one shared back half:

- `infracost JSON (or HTML)` → `schema` → `(optional) aws enrichment` → `report`
- `terraform show -json` → `tf_state` → `pricer` (+ SQLite price cache) → `report`

- **`bucksawz/schema/infracost.py`** — dataclass schema for Infracost's JSON output: `InfracostOutput → Project → Breakdown → Resource → CostComponent`. This is the canonical in-memory representation everything else operates on. `CostComponent.usage_based` (derived from `monthlyCost is None and price is not None`, or an explicit flag) marks components that need CloudWatch/estimation data rather than a static Infracost cost.
- **`bucksawz/schema/html_parser.py`** — reconstructs the same dataclass schema by scraping an existing Infracost HTML report, for when only a historical HTML export is available (no JSON).
- **`bucksawz/aws/costexplorer.py`** + **`bucksawz/aws/cloudwatch.py`** — the `enrich` command. Cost Explorer supplies historical actuals and per-account breakdowns (from AWS Organizations consolidated billing); CloudWatch supplies usage metrics (LCUs, request counts, invocations) for usage-based resources. Enrichment output is the same JSON schema with extra fields (`estimatedMonthlyCost` per resource, `historical.monthlyAverageByAccount`) that `report` picks up automatically if present.
- **`bucksawz/aws/cache.py`** — generic JSON disk cache (`~/.cache/bucksawz/*.json`, sha256-keyed, default 7-day TTL) used by Cost Explorer/CloudWatch calls to avoid re-hitting AWS on repeated runs. Independent from the pricing SQLite cache below.
- **`bucksawz/pricing/`** — the standalone pricing engine:
  - `db.py` — SQLite price cache at `~/.cache/bucksawz/prices.db` (`service, region, price_key → price_usd`), populated via `fetcher.py` calling the AWS Pricing API (`pricing:GetProducts`, global endpoint in us-east-1).
  - `fetcher.py` — one fetcher per service (9 today, registered in `_FETCHERS`), each translating Pricing API products into `price_key`s. **The client-side filtering is the fragile part**: many services have adjacent line items sharing a usagetype suffix or group substring (Outposts/Trust Store ELB rates, Lambda ephemeral-storage duration, ElastiCache Extended Support, S3 storage classes collapsed under `storageClass`) which will silently overwrite the real regional price if not excluded. Prefer exact matches over substring tests, and add a rejection test in `tests/test_fetcher.py` for every contaminant found.
  - `estimator.py` — matches usage-based `CostComponent`s to CloudWatch actuals (by unit/name heuristics — LCU, request/invocation counts) and computes `estimated_monthly_cost = normalized_monthly_quantity × price`. This is what backs `enrich`'s per-resource estimates. Note it works in *millions* of requests, so `pricer.py` scales the Pricing API's per-request rates by 1e6 to match.
  - `tf_state.py` — parses `terraform show -json` (plan or state) into flat `TFResource` configs, walking nested modules.
  - `pricer.py` — maps `TFResource`s to priced `Resource`s via `_PRICERS` (keyed on Terraform type) and the price cache, then `build_output` wraps them in the same `InfracostOutput` the report consumes. Unmatched resources become `no_price` rather than being dropped; unrecognised *types* are skipped entirely. Costs that can't be derived from config alone (S3 bucket size, request counts) are emitted as usage-based components with a unit price and no total.
- **`bucksawz/report/render.py`** — Jinja2 template → single self-contained HTML file (CSS/JS/Chart.js all inlined, no CDN references) with sidebar ToC, charts, collapsible sections, and client-side search. Reads enrichment fields (`estimatedMonthlyCost`, account breakdown) directly off the `InfracostOutput` JSON if present.
- **`bucksawz/cli.py`** — Click CLI wiring the above together (`report`, `enrich`, `prices update/info`, `price-state`, `cache clear/info`, `from-html`, `html-to-json`). AWS-dependent imports are deliberately deferred into the command bodies to keep CLI startup fast.

## Notes

- `DECISIONS.md` records the rationale behind major architectural choices (Python over Go, JSON schema as canonical input, self-contained HTML output, etc.) — check it before revisiting a past decision.
- The GitHub Actions workflow (`.github/workflows/`) is the reference integration: Infracost → optional OIDC-based `enrich` → `report` → upload artifact + PR comment.
- This repo keeps AI attribution in commit messages (`Co-Authored-By:` trailers), overriding the global "never add AI attribution" instruction.

<!-- MEMORY:START -->
# bucksawz

_Last updated: 2026-07-27 | 0 active memories, 0 total_

_For deeper context, use memory_search, memory_related, or memory_ask tools._
<!-- MEMORY:END -->
