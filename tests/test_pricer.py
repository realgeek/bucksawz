"""Tests for pricing terraform resource configs directly against the SQLite price cache."""
import pytest
from pathlib import Path
from bucksawz.pricing import db as price_db
from bucksawz.pricing.estimator import estimate_resource_cost
from bucksawz.pricing.pricer import (
    apply_ec2_runtime_actuals,
    apply_elasticache_runtime_actuals,
    build_multi_project_output,
    build_output,
    estimate_data_transfer_cost,
    estimate_elb_lcu_cost,
    estimate_rds_storage_cost,
    estimate_s3_storage_cost,
    extrapolate_by_resource_type,
    find_new_resources,
    price_data_transfer,
    price_resources,
    price_terraform_json,
)
from bucksawz.pricing.tf_state import TFResource
from bucksawz.schema.infracost import Resource


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    db = tmp_path / "test_prices.db"
    price_db.upsert("AmazonEC2", "us-east-1", "ec2:t3.micro:linux:shared", "Hrs", 0.0104, db=db)
    price_db.upsert("AmazonRDS", "us-east-1", "rds:db.t3.medium:PostgreSQL:Single-AZ", "Hrs", 0.068, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu", "vCPU-Hours", 0.04048, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory", "GB-Hours", 0.004445, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:vcpu:arm", "vCPU-Hours", 0.03238, db=db)
    price_db.upsert("AmazonECS", "us-east-1", "fargate:memory:arm", "GB-Hours", 0.003556, db=db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:requests", "Requests", 2e-7, db=db)
    price_db.upsert("AWSLambda", "us-east-1", "lambda:duration:x86_64", "GB-Seconds", 1.6667e-5, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:hourly:application", "Hrs", 0.0243, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:lcu:application", "LCU-Hrs", 0.008, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:hourly:classic", "Hrs", 0.027, db=db)
    price_db.upsert("AWSELB", "us-east-1", "elb:data:classic", "GB", 0.008, db=db)
    price_db.upsert("AmazonElastiCache", "us-east-1", "elasticache:cache.t3.micro:redis", "Hrs", 0.017, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3:storage:standard", "GB-Mo", 0.023, db=db)
    price_db.upsert("AWSQueueService", "us-east-1", "sqs:requests:standard", "Requests", 4e-7, db=db)
    price_db.upsert("AWSQueueService", "us-east-1", "sqs:requests:fifo", "Requests", 5e-7, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:gp3", "GB-Mo", 0.08, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:gp2", "GB-Mo", 0.10, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:io1", "GB-Mo", 0.125, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:io2", "GB-Mo", 0.125, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:gp3", "IOPS-Mo", 0.005, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io1", "IOPS-Mo", 0.065, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier1", "IOPS-Mo", 0.065, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier2", "IOPS-Mo", 0.0455, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:iops:io2:tier3", "IOPS-Mo", 0.03185, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:throughput:gp3", "MiBps-Mo", 0.04, db=db)
    price_db.upsert("AWSSecretsManager", "us-east-1", "secretsmanager:secret", "Secrets", 0.40, db=db)
    price_db.upsert("AWSSecretsManager", "us-east-1", "secretsmanager:requests", "API Requests", 5e-6, db=db)
    price_db.upsert("AmazonRoute53", "us-east-1", "route53:hostedzone", "HostedZone", 0.50, db=db)
    price_db.upsert("AmazonRoute53", "us-east-1", "route53:queries", "Queries", 4e-7, db=db)
    price_db.upsert("awskms", "us-east-1", "kms:key", "Keys", 1.0, db=db)
    price_db.upsert("awskms", "us-east-1", "kms:requests", "Requests", 3e-6, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:webacl", "Month", 5.0, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:rule", "Month", 1.0, db=db)
    price_db.upsert("awswaf", "us-east-1", "waf:requests", "Request", 6e-7, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "natgateway:hourly", "Hrs", 0.045, db=db)
    price_db.upsert("AmazonEC2", "us-east-1", "natgateway:data", "GB", 0.045, db=db)
    price_db.upsert("AWSConfig", "us-east-1", "config:item", "items", 0.003, db=db)
    price_db.upsert("AWSConfig", "us-east-1", "config:rule:evaluation", "evaluations", 0.001, db=db)
    price_db.upsert("AmazonCloudWatch", "us-east-1", "cloudwatch:alarm", "Alarms", 0.10, db=db)
    price_db.upsert("AmazonCloudWatch", "us-east-1", "cloudwatch:logs:ingestion", "GB", 0.50, db=db)
    price_db.upsert("AmazonCloudWatch", "us-east-1", "cloudwatch:logs:storage", "GB-Mo", 0.03, db=db)
    price_db.upsert("AmazonEKS", "us-east-1", "eks:cluster", "Hrs", 0.10, db=db)
    price_db.upsert("AmazonDynamoDB", "us-east-1", "dynamodb:storage", "GB-Mo", 0.25, db=db)
    price_db.upsert("AmazonDynamoDB", "us-east-1", "dynamodb:provisioned:read", "RCU-Hrs", 0.00013, db=db)
    price_db.upsert("AmazonDynamoDB", "us-east-1", "dynamodb:provisioned:write", "WCU-Hrs", 0.00065, db=db)
    price_db.upsert("AmazonDynamoDB", "us-east-1", "dynamodb:ondemand:read", "Requests", 1.25e-7, db=db)
    price_db.upsert("AmazonDynamoDB", "us-east-1", "dynamodb:ondemand:write", "Requests", 6.25e-7, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "vpcendpoint:hourly", "Hrs", 0.01, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "vpcendpoint:data", "GB", 0.01, db=db)
    price_db.upsert("AmazonSNS", "us-east-1", "sns:requests", "Requests", 5e-7, db=db)
    price_db.upsert("AmazonEFS", "us-east-1", "efs:storage:standard", "GB-Mo", 0.30, db=db)
    price_db.upsert("AmazonECR", "us-east-1", "ecr:storage", "GB-Mo", 0.10, db=db)
    price_db.upsert("AmazonApiGateway", "us-east-1", "apigateway:rest:requests", "Requests", 3.5e-6, db=db)
    price_db.upsert("AmazonApiGateway", "us-east-1", "apigateway:http:requests", "Requests", 1.0e-6, db=db)
    price_db.upsert("AmazonCloudFront", "us-east-1", "cloudfront:data:out", "GB", 0.085, db=db)
    price_db.upsert("AmazonCloudFront", "us-east-1", "cloudfront:requests:https", "Requests", 1.0e-5, db=db)
    price_db.upsert("AmazonKinesis", "us-east-1", "kinesis:shard:hour", "Hrs", 0.015, db=db)
    price_db.upsert("AmazonKinesis", "us-east-1", "kinesis:payload:units", "Units", 1.4e-8, db=db)
    price_db.upsert("AmazonStates", "us-east-1", "sfn:standard:transitions", "Transitions", 2.5e-5, db=db)
    price_db.upsert("AmazonStates", "us-east-1", "sfn:express:requests", "Requests", 1e-6, db=db)
    price_db.upsert("AmazonStates", "us-east-1", "sfn:express:duration", "GB-Second", 1.042e-5, db=db)
    price_db.upsert("AWSEvents", "us-east-1", "eventbridge:events", "Events", 1e-6, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "transitgateway:hourly", "Hrs", 0.05, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "transitgateway:data", "GB", 0.02, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3files:storage", "GB-Mo", 0.30, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3files:write", "GB", 0.06, db=db)
    price_db.upsert("AmazonS3", "us-east-1", "s3files:read", "GB", 0.03, db=db)
    price_db.upsert("AmazonES", "us-east-1", "opensearch:r6g.large.elasticsearch", "Hrs", 0.167, db=db)
    price_db.upsert("AmazonES", "us-east-1", "opensearch:storage:gp2", "GB-Mo", 0.135, db=db)
    price_db.upsert("AmazonES", "us-east-1", "opensearch:storage:gp3", "GB-Mo", 0.112, db=db)
    price_db.upsert("AmazonRedshift", "us-east-1", "redshift:ra3.xlplus", "Hrs", 1.086, db=db)
    price_db.upsert("AmazonRedshift", "us-east-1", "redshift:dc2.large", "Hrs", 0.25, db=db)
    price_db.upsert("AmazonRedshift", "us-east-1", "redshift:storage", "GB-Mo", 0.024, db=db)
    price_db.upsert("AWSBackup", "us-east-1", "backup:storage:warm", "GB-Mo", 0.05, db=db)
    price_db.upsert("AWSBackup", "us-east-1", "backup:storage:cold", "GB-Mo", 0.01, db=db)
    price_db.upsert("AWSBackup", "us-east-1", "backup:restore", "GB", 0.02, db=db)
    price_db.upsert("AmazonMSK", "us-east-1", "msk:kafka.m5.large", "Hrs", 0.21, db=db)
    price_db.upsert("AmazonMSK", "us-east-1", "msk:storage", "GB-Mo", 0.10, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "eip:hourly", "Hrs", 0.005, db=db)
    price_db.upsert("AWSCloudTrail", "us-east-1", "cloudtrail:management", "Events", 2.00, db=db)
    price_db.upsert("AWSCloudTrail", "us-east-1", "cloudtrail:data", "Events", 0.10, db=db)
    price_db.upsert("AWSCloudTrail", "us-east-1", "cloudtrail:insights", "Events", 0.35, db=db)
    price_db.upsert("AmazonGuardDuty", "us-east-1", "guardduty:analysis", "GB", 4.00, db=db)
    price_db.upsert("AmazonDocDB", "us-east-1", "docdb:db.r5.large", "Hrs", 0.277, db=db)
    price_db.upsert("AmazonFSx", "us-east-1", "fsx:windows:storage:ssd", "GB-Mo", 0.13, db=db)
    price_db.upsert("AmazonFSx", "us-east-1", "fsx:windows:storage:hdd", "GB-Mo", 0.013, db=db)
    price_db.upsert("AmazonFSx", "us-east-1", "fsx:windows:throughput", "MBps-Mo", 2.20, db=db)
    price_db.upsert("AWSCertificateManager", "us-east-1", "acmpca:monthly:general_purpose", "Mo", 400.00, db=db)
    price_db.upsert("AWSCertificateManager", "us-east-1", "acmpca:monthly:short_lived", "Mo", 50.00, db=db)
    price_db.upsert("AWSCertificateManager", "us-east-1", "acmpca:certificate", "Certificates", 0.75, db=db)
    price_db.upsert("AmazonAthena", "us-east-1", "athena:scanned", "TB", 5.00, db=db)
    price_db.upsert("AmazonFSx", "us-east-1", "fsx:lustre:SCRATCH2:SSD", "GB-Mo", 0.14, db=db)
    price_db.upsert("AmazonFSx", "us-east-1", "fsx:lustre:PERSISTENT1:HDD", "GB-Mo", 0.025, db=db)
    price_db.upsert("AmazonNeptune", "us-east-1", "neptune:db.r5.large", "Hrs", 0.348, db=db)
    price_db.upsert("AWSGlobalAccelerator", "us-east-1", "globalaccelerator:hourly", "Hrs", 0.025, db=db)
    price_db.upsert("AWSGlobalAccelerator", "us-east-1", "globalaccelerator:data", "GB", 0.015, db=db)
    price_db.upsert("AmazonMQ", "us-east-1", "mq:mq.m5.large", "Hrs", 0.30, db=db)
    price_db.upsert("AmazonMQ", "us-east-1", "mq:storage", "GB-Mo", 0.30, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "vpn:sitetosite:hourly", "Hrs", 0.05, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "vpn:clientvpn:association:hourly", "Hrs", 0.10, db=db)
    price_db.upsert("AmazonVPC", "us-east-1", "vpn:clientvpn:connection:hourly", "Hrs", 0.05, db=db)
    price_db.upsert("AWSDirectConnect", "us-east-1", "directconnect:port:1gbps", "Hrs", 0.30, db=db)
    price_db.upsert("AWSDirectConnect", "us-east-1", "directconnect:port:10gbps", "Hrs", 2.25, db=db)
    price_db.upsert("AWSAppSync", "us-east-1", "appsync:requests", "requests", 4.0, db=db)
    price_db.upsert("AWSAppSync", "us-east-1", "appsync:connectionminutes", "minutes", 0.00002, db=db)
    price_db.upsert("AmazonCognito", "us-east-1", "cognito:mau", "users", 0.0055, db=db)
    price_db.upsert("AWSGlue", "us-east-1", "glue:dpuhour", "DPU-Hour", 0.44, db=db)
    price_db.upsert("AmazonSageMaker", "us-east-1", "sagemaker:ml.t3.medium", "Hrs", 0.0582, db=db)
    price_db.upsert("AmazonSageMaker", "us-east-1", "sagemaker:ml.m5.xlarge", "Hrs", 0.269, db=db)
    price_db.upsert("CloudHSM", "us-east-1", "cloudhsm:hourly", "Hrs", 1.60, db=db)
    price_db.upsert("AmazonMacie", "us-east-1", "macie:gb", "GB", 1.00, db=db)
    price_db.upsert("AmazonInspectorV2", "us-east-1", "inspector:ec2", "months", 0.01, db=db)
    price_db.upsert("AmazonInspectorV2", "us-east-1", "inspector:ecr", "images", 0.09, db=db)
    price_db.upsert("AmazonInspectorV2", "us-east-1", "inspector:lambda", "months", 0.30, db=db)
    return db


@pytest.fixture
def empty_db(tmp_path) -> Path:
    """A price cache with no rows — exercises the no-price / fallback paths."""
    return tmp_path / "empty_prices.db"


def _tf(type_, values, address=None, region=None):
    return TFResource(
        address=address or f"{type_}.thing",
        type=type_,
        name="thing",
        provider_name="registry.terraform.io/hashicorp/aws",
        values=values,
        region=region,
    )


def test_ec2_instance_priced(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)


def test_resource_own_region_overrides_cli_region(tmp_db):
    """A resource with a resolved provider region prices against that
    region's cache rows, even when a different --region is passed."""
    price_db.upsert(
        "AmazonEC2", "eu-west-1", "ec2:t3.micro:linux:shared", "Hrs", 0.0119, db=tmp_db
    )
    tf = _tf("aws_instance", {"instance_type": "t3.micro"}, region="eu-west-1")
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0119 * 730)


def test_resource_without_own_region_falls_back_to_cli_region(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"}, region=None)
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)


def test_ec2_missing_instance_type_unpriced(tmp_db):
    tf = _tf("aws_instance", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert not resource.is_supported
    assert resource.no_price


def test_ec2_no_price_data_for_region(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "m5.24xlarge"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_rds_instance_priced(tmp_db):
    tf = _tf("aws_db_instance", {"instance_class": "db.t3.medium", "engine": "postgres", "multi_az": False})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.068 * 730)


def test_rds_unsupported_engine_unpriced(tmp_db):
    tf = _tf("aws_db_instance", {"instance_class": "db.t3.medium", "engine": "oracle-se2"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_lb_priced_with_usage_based_lcu(tmp_db):
    tf = _tf("aws_lb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)  # cached rate, not the fallback
    lcu = next(c for c in resource.cost_components if c.usage_based)
    assert lcu.unit == "LCU"
    assert lcu.price == pytest.approx(0.008)
    assert lcu.monthly_cost is None


def test_lb_falls_back_to_flat_rate_without_cached_price(empty_db):
    """`prices update --services ELB` not run yet: approximate rather than drop the cost."""
    tf = _tf("aws_lb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.monthly_cost == pytest.approx(0.0225 * 730)
    lcu = next(c for c in resource.cost_components if c.usage_based)
    assert lcu.price == pytest.approx(0.008)


def test_lb_unknown_type_treated_as_application(tmp_db):
    tf = _tf("aws_lb", {"load_balancer_type": "quantum"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)


def test_classic_elb_bills_data_processed_not_lcus(tmp_db):
    tf = _tf("aws_elb", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.027 * 730)
    variable = next(c for c in resource.cost_components if c.usage_based)
    assert variable.unit == "GB"
    assert variable.price == pytest.approx(0.008)


def test_nat_gateway_priced_with_usage_based_data_processed(tmp_db):
    tf = _tf("aws_nat_gateway", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.045 * 730)
    variable = next(c for c in resource.cost_components if c.usage_based)
    assert variable.unit == "GB"
    assert variable.price == pytest.approx(0.045)
    assert variable.monthly_cost is None


def test_nat_gateway_falls_back_to_flat_rate_without_cached_price(empty_db):
    tf = _tf("aws_nat_gateway", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.monthly_cost == pytest.approx(0.045 * 730)
    variable = next(c for c in resource.cost_components if c.usage_based)
    assert variable.price is None


def test_config_recorder_fully_usage_based(tmp_db):
    tf = _tf("aws_config_configuration_recorder", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.unit == "items"
    assert comp.price == pytest.approx(0.003)
    assert comp.monthly_cost is None


def test_config_rule_fully_usage_based(tmp_db):
    tf = _tf("aws_config_config_rule", {"source": {"owner": "AWS", "source_identifier": "S3_BUCKET_PUBLIC_READ_PROHIBITED"}})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.unit == "evaluations"
    assert comp.price == pytest.approx(0.001)


def test_config_falls_back_to_documented_rate_without_cached_price(empty_db):
    tf = _tf("aws_config_configuration_recorder", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    [comp] = resource.cost_components
    assert comp.price == pytest.approx(0.003)


def test_cloudwatch_alarm_flat_monthly_cost(tmp_db):
    tf = _tf("aws_cloudwatch_metric_alarm", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.10)
    [comp] = resource.cost_components
    assert not comp.usage_based
    assert comp.monthly_cost == pytest.approx(0.10)


def test_cloudwatch_alarm_falls_back_without_cached_price(empty_db):
    tf = _tf("aws_cloudwatch_metric_alarm", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.monthly_cost == pytest.approx(0.10)


def test_cloudwatch_log_group_both_components_usage_based(tmp_db):
    tf = _tf("aws_cloudwatch_log_group", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    names = {c.name: c for c in resource.cost_components}
    assert set(names) == {"Data ingested", "Data stored"}
    assert names["Data ingested"].price == pytest.approx(0.50)
    assert names["Data stored"].price == pytest.approx(0.03)
    assert all(c.monthly_cost is None for c in resource.cost_components)


def test_cloudwatch_log_group_unpriced_without_cached_data(empty_db):
    tf = _tf("aws_cloudwatch_log_group", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_alb_alias_is_priced(tmp_db):
    tf = _tf("aws_alb", {"load_balancer_type": "application"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0243 * 730)


def test_ecs_fargate_x86_priced(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "runtime_platform": [{"cpu_architecture": "X86_64"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.5 * 730 * 0.04048) + (1.0 * 730 * 0.004445)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ecs_fargate_arm_uses_arm_prices(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "runtime_platform": [{"cpu_architecture": "ARM64"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.5 * 730 * 0.03238) + (1.0 * 730 * 0.003556)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ecs_non_fargate_skipped(tmp_db):
    tf = _tf("aws_ecs_task_definition", {
        "requires_compatibilities": ["EC2"],
        "cpu": "512",
        "memory": "1024",
    })
    assert price_resources([tf], "us-east-1", db=tmp_db) == []


def test_lambda_priced_as_usage_based(tmp_db):
    tf = _tf("aws_lambda_function", {"memory_size": 256, "architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    assert len(resource.cost_components) == 2
    assert all(c.usage_based for c in resource.cost_components)
    duration = next(c for c in resource.cost_components if "Duration" in c.name)
    assert duration.price == pytest.approx(1.6667e-5)


def test_lambda_request_price_normalised_to_millions(tmp_db):
    """
    The Pricing API quotes SQS/Lambda requests per single request, but the unit
    reported is "1M requests" — and estimator.py multiplies millions of requests
    by that price, so the price must be scaled to match or the estimate is 1e6 too low.
    """
    tf = _tf("aws_lambda_function", {"architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    requests = next(c for c in resource.cost_components if c.name == "Requests")
    assert requests.unit == "1M requests"
    assert requests.price == pytest.approx(0.20)


def test_lambda_estimate_matches_cloudwatch_actuals(tmp_db):
    """End-to-end unit check on the pricer → estimator handoff."""
    tf = _tf("aws_lambda_function", {"architectures": ["x86_64"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    # 6M invocations over 90 days → 2M/month × $0.20/M = $0.40/mo
    est = estimate_resource_cost(resource, {"Invocations": 6.0}, lookback_days=90)
    assert est == pytest.approx(0.40, rel=1e-4)


# ── ElastiCache ──────────────────────────────────────────────────────────────


def test_elasticache_cluster_priced_per_node(tmp_db):
    tf = _tf("aws_elasticache_cluster", {
        "node_type": "cache.t3.micro", "engine": "redis", "num_cache_nodes": 2,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 2 * 730)
    assert resource.cost_components[0].hourly_quantity == pytest.approx(2.0)


def test_elasticache_defaults_to_redis_and_one_node(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"node_type": "cache.t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 730)


def test_elasticache_replication_group_uses_cluster_count(tmp_db):
    tf = _tf("aws_elasticache_replication_group", {
        "node_type": "cache.t3.micro", "num_cache_clusters": 3,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 3 * 730)


def test_elasticache_cluster_mode_counts_shards_and_replicas(tmp_db):
    """2 shards × (1 primary + 1 replica) = 4 billed nodes."""
    tf = _tf("aws_elasticache_replication_group", {
        "node_type": "cache.t3.micro", "num_node_groups": 2, "replicas_per_node_group": 1,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.017 * 4 * 730)


def test_elasticache_missing_node_type_unpriced(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"engine": "redis"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_elasticache_uncached_engine_unpriced(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"node_type": "cache.t3.micro", "engine": "memcached"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── S3 / SQS ─────────────────────────────────────────────────────────────────


def test_s3_bucket_priced_as_usage_based_storage(tmp_db):
    """Bucket size isn't in the terraform config, so storage stays usage-based."""
    tf = _tf("aws_s3_bucket", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.unit == "GB-months"
    assert comp.price == pytest.approx(0.023)


def test_s3_bucket_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_s3_bucket", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_sqs_standard_queue_priced_per_million(tmp_db):
    tf = _tf("aws_sqs_queue", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    [comp] = resource.cost_components
    assert comp.unit == "1M requests"
    assert comp.price == pytest.approx(0.40)


def test_sqs_fifo_queue_uses_fifo_price(tmp_db):
    tf = _tf("aws_sqs_queue", {"fifo_queue": True})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.cost_components[0].price == pytest.approx(0.50)


# ── Secrets Manager ───────────────────────────────────────────────────────────


def test_secretsmanager_secret_flat_monthly_cost(tmp_db):
    """Unlike S3/SQS, a secret's price doesn't depend on its config or usage."""
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.40)


def test_secretsmanager_secret_has_fixed_and_usage_based_components(tmp_db):
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    fixed, requests = resource.cost_components
    assert not fixed.usage_based
    assert fixed.monthly_cost == pytest.approx(0.40)
    assert requests.usage_based
    assert requests.monthly_cost is None
    assert requests.unit == "1M requests"
    assert requests.price == pytest.approx(5.0)


def test_secretsmanager_secret_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_secretsmanager_secret", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Route 53 ─────────────────────────────────────────────────────────────────


def test_route53_zone_flat_monthly_cost(tmp_db):
    """Like a Secrets Manager secret, a hosted zone's base price is known from
    config alone; query volume is not."""
    tf = _tf("aws_route53_zone", {"name": "example.com"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.50)
    fixed, queries = resource.cost_components
    assert not fixed.usage_based
    assert queries.usage_based
    assert queries.monthly_cost is None
    assert queries.price == pytest.approx(0.40)


def test_route53_zone_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_route53_zone", {"name": "example.com"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── KMS ──────────────────────────────────────────────────────────────────────


def test_kms_key_flat_monthly_cost(tmp_db):
    tf = _tf("aws_kms_key", {"description": "app secrets"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(1.0)
    fixed, requests = resource.cost_components
    assert not fixed.usage_based
    assert requests.usage_based
    assert requests.price == pytest.approx(3.0)


def test_kms_key_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_kms_key", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── WAF ──────────────────────────────────────────────────────────────────────


def test_waf_web_acl_with_no_rules(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(5.0)
    names = [c.name for c in resource.cost_components]
    assert names == ["Web ACL", "Requests"]


def test_waf_web_acl_rule_count_folded_into_total(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {
        "name": "api-waf",
        "rule": [{"name": "rate-limit"}, {"name": "sql-injection"}, {"name": "geo-block"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(5.0 + 3 * 1.0)
    rules_comp = next(c for c in resource.cost_components if c.name.startswith("Rules"))
    assert rules_comp.monthly_quantity == 3.0
    assert rules_comp.monthly_cost == pytest.approx(3.0)


def test_waf_web_acl_bare_dict_single_rule(tmp_db):
    """A single `rule` block can show up as a bare dict rather than a list of one."""
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf", "rule": {"name": "rate-limit"}})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(5.0 + 1.0)


def test_waf_web_acl_request_component_is_usage_based(tmp_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    requests_comp = next(c for c in resource.cost_components if c.name == "Requests")
    assert requests_comp.usage_based
    assert requests_comp.monthly_cost is None
    assert requests_comp.price == pytest.approx(0.60)


def test_waf_web_acl_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_wafv2_web_acl", {"name": "api-waf"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── EBS: standalone aws_ebs_volume ───────────────────────────────────────────


def test_ebs_volume_gp3_storage_only(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)
    [comp] = resource.cost_components
    assert comp.name == "Storage (gp3, 100 GB)"


def test_ebs_volume_defaults_to_gp2_when_type_missing(tmp_db):
    tf = _tf("aws_ebs_volume", {"size": 50})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(50 * 0.10)


def test_ebs_volume_missing_size_is_unpriced(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_ebs_volume_uncached_type_is_unpriced(empty_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_ebs_gp3_iops_below_baseline_is_free(tmp_db):
    """3,000 IOPS is included in gp3's storage price."""
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 3000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)
    assert len(resource.cost_components) == 1


def test_ebs_gp3_iops_above_baseline_billed_on_the_excess(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 4000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (1000 * 0.005)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_gp3_throughput_below_baseline_is_free(tmp_db):
    """125 MiB/s is included in gp3's storage price."""
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "throughput": 125})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(100 * 0.08)


def test_ebs_gp3_throughput_above_baseline_billed_on_the_excess(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "throughput": 500})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (375 * 0.04)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_gp3_iops_and_throughput_both_billed(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "gp3", "size": 100, "iops": 5000, "throughput": 250})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.08) + (2000 * 0.005) + (125 * 0.04)
    assert resource.monthly_cost == pytest.approx(expected)
    assert len(resource.cost_components) == 3


def test_ebs_io1_iops_has_no_free_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io1", "size": 100, "iops": 1000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (1000 * 0.065)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_within_first_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 10000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (10000 * 0.065)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_blends_across_tier_boundaries(tmp_db):
    """40,000 IOPS = 32,000 @ tier1 + 8,000 @ tier2 — not 40,000 at either rate."""
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 40000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (32000 * 0.065) + (8000 * 0.0455)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_io2_iops_reaches_third_tier(tmp_db):
    tf = _tf("aws_ebs_volume", {"type": "io2", "size": 100, "iops": 70000})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (100 * 0.125) + (32000 * 0.065) + (32000 * 0.0455) + (6000 * 0.03185)
    assert resource.monthly_cost == pytest.approx(expected)


def test_ebs_st1_has_no_iops_component(tmp_db):
    """st1/sc1/standard don't take a separate IOPS charge even if given one."""
    price_db.upsert("AmazonEC2", "us-east-1", "ebs:storage:st1", "GB-Mo", 0.045, db=tmp_db)
    tf = _tf("aws_ebs_volume", {"type": "st1", "size": 500, "iops": 500})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(500 * 0.045)
    assert len(resource.cost_components) == 1


# ── EBS: attached to aws_instance / aws_launch_template ──────────────────────


def test_instance_root_volume_adds_to_total(tmp_db):
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "gp3", "volume_size": 20}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (20 * 0.08)
    assert resource.monthly_cost == pytest.approx(expected)
    [root] = resource.sub_resources
    assert root.monthly_cost == pytest.approx(20 * 0.08)


def test_instance_root_block_device_as_bare_dict(tmp_db):
    """Some state exports give root_block_device as a single dict, not a list."""
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": {"volume_type": "gp3", "volume_size": 20},
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert len(resource.sub_resources) == 1


def test_instance_additional_ebs_block_devices(tmp_db):
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "gp3", "volume_size": 20}],
        "ebs_block_device": [
            {"device_name": "/dev/sdf", "volume_type": "gp2", "volume_size": 100},
            {"device_name": "/dev/sdg", "volume_type": "gp2", "volume_size": 200},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (20 * 0.08) + (100 * 0.10) + (200 * 0.10)
    assert resource.monthly_cost == pytest.approx(expected)
    assert len(resource.sub_resources) == 3
    names = [s.name for s in resource.sub_resources]
    assert any("/dev/sdf" in n for n in names)
    assert any("/dev/sdg" in n for n in names)


def test_instance_with_no_block_devices_is_unaffected(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)
    assert resource.sub_resources == []


def test_launch_template_block_device_mappings(tmp_db):
    tf = _tf("aws_launch_template", {
        "instance_type": "t3.micro",
        "block_device_mappings": [
            {"device_name": "/dev/xvda", "ebs": [{"volume_type": "gp3", "volume_size": 30}]},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    expected = (0.0104 * 730) + (30 * 0.08)
    assert resource.monthly_cost == pytest.approx(expected)


def test_launch_template_no_device_mapping_is_skipped(tmp_db):
    """A mapping with no `ebs` block (ephemeral / no_device) isn't an EBS volume."""
    tf = _tf("aws_launch_template", {
        "instance_type": "t3.micro",
        "block_device_mappings": [
            {"device_name": "ephemeral0", "virtual_name": "ephemeral0", "ebs": []},
        ],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.sub_resources == []


def test_instance_unpriced_block_device_still_visible(tmp_db):
    """An EBS type with no cached price shows up as no_price, not silently dropped."""
    tf = _tf("aws_instance", {
        "instance_type": "t3.micro",
        "root_block_device": [{"volume_type": "sc1", "volume_size": 20}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0104 * 730)  # instance cost unaffected
    [root] = resource.sub_resources
    assert root.no_price


# ── EKS ──────────────────────────────────────────────────────────────────────


def test_eks_cluster_flat_hourly_cost(tmp_db):
    tf = _tf("aws_eks_cluster", {"name": "prod"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.10 * 730)
    [comp] = resource.cost_components
    assert not comp.usage_based


def test_eks_cluster_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_eks_cluster", {"name": "prod"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── DynamoDB ─────────────────────────────────────────────────────────────────


def test_dynamodb_provisioned_known_capacity_cost(tmp_db):
    tf = _tf("aws_dynamodb_table", {
        "name": "orders", "billing_mode": "PROVISIONED",
        "read_capacity": 5, "write_capacity": 2,
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    expected = 5 * 0.00013 * 730 + 2 * 0.00065 * 730
    assert resource.monthly_cost == pytest.approx(expected)
    names = [c.name for c in resource.cost_components]
    assert names == ["Storage", "Provisioned read capacity", "Provisioned write capacity"]
    storage = resource.cost_components[0]
    assert storage.usage_based
    assert storage.monthly_cost is None


def test_dynamodb_provisioned_is_default_billing_mode(tmp_db):
    """terraform's own default when `billing_mode` is omitted."""
    tf = _tf("aws_dynamodb_table", {"name": "orders", "read_capacity": 1, "write_capacity": 1})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.00013 * 730 + 0.00065 * 730)


def test_dynamodb_ondemand_is_usage_based(tmp_db):
    tf = _tf("aws_dynamodb_table", {"name": "orders", "billing_mode": "PAY_PER_REQUEST"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = [c.name for c in resource.cost_components]
    assert names == ["Storage", "On-demand read requests", "On-demand write requests"]
    read_comp = resource.cost_components[1]
    assert read_comp.usage_based
    assert read_comp.price == pytest.approx(1.25e-7 * 1_000_000)


def test_dynamodb_unpriced_without_cached_storage_price(empty_db):
    tf = _tf("aws_dynamodb_table", {"name": "orders"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── VPC Interface Endpoint ───────────────────────────────────────────────────


def test_vpc_gateway_endpoint_is_free(tmp_db):
    tf = _tf("aws_vpc_endpoint", {"vpc_endpoint_type": "Gateway", "service_name": "com.amazonaws.us-east-1.s3"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0)


def test_vpc_gateway_is_default_endpoint_type(tmp_db):
    tf = _tf("aws_vpc_endpoint", {"service_name": "com.amazonaws.us-east-1.dynamodb"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.0)


def test_vpc_interface_endpoint_priced_per_az(tmp_db):
    tf = _tf("aws_vpc_endpoint", {
        "vpc_endpoint_type": "Interface",
        "service_name": "com.amazonaws.us-east-1.ec2",
        "subnet_ids": ["subnet-1", "subnet-2"],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.01 * 2 * 730)
    fixed, data = resource.cost_components
    assert not fixed.usage_based
    assert data.usage_based
    assert data.price == pytest.approx(0.01)


def test_vpc_interface_endpoint_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_vpc_endpoint", {"vpc_endpoint_type": "Interface", "subnet_ids": ["subnet-1"]})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── SNS ──────────────────────────────────────────────────────────────────────


def test_sns_topic_is_usage_based(tmp_db):
    tf = _tf("aws_sns_topic", {"name": "alerts"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(5e-7 * 1_000_000)


def test_sns_topic_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_sns_topic", {"name": "alerts"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── EFS ──────────────────────────────────────────────────────────────────────


def test_efs_file_system_is_usage_based(tmp_db):
    tf = _tf("aws_efs_file_system", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(0.30)


def test_efs_file_system_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_efs_file_system", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── ECR ──────────────────────────────────────────────────────────────────────


def test_ecr_repository_is_usage_based(tmp_db):
    tf = _tf("aws_ecr_repository", {"name": "app"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(0.10)


def test_ecr_repository_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_ecr_repository", {"name": "app"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── API Gateway ──────────────────────────────────────────────────────────────


def test_api_gateway_rest_api_is_usage_based(tmp_db):
    tf = _tf("aws_api_gateway_rest_api", {"name": "api"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(3.5e-6 * 1_000_000)


def test_api_gateway_rest_api_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_api_gateway_rest_api", {"name": "api"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_api_gateway_v2_http_api_is_usage_based(tmp_db):
    tf = _tf("aws_apigatewayv2_api", {"name": "api", "protocol_type": "HTTP"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.price == pytest.approx(1.0e-6 * 1_000_000)


def test_api_gateway_v2_websocket_unsupported(tmp_db):
    tf = _tf("aws_apigatewayv2_api", {"name": "api", "protocol_type": "WEBSOCKET"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── CloudFront ───────────────────────────────────────────────────────────────


def test_cloudfront_distribution_is_usage_based(tmp_db):
    tf = _tf("aws_cloudfront_distribution", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    data, requests = resource.cost_components
    assert data.usage_based and data.price == pytest.approx(0.085)
    assert requests.usage_based and requests.price == pytest.approx(1.0e-5 * 1_000_000)


def test_cloudfront_distribution_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_cloudfront_distribution", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Kinesis ──────────────────────────────────────────────────────────────────


def test_kinesis_provisioned_known_shard_cost(tmp_db):
    tf = _tf("aws_kinesis_stream", {"name": "events", "shard_count": 4})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(4 * 0.015 * 730)
    fixed, payload = resource.cost_components
    assert not fixed.usage_based
    assert payload.usage_based


def test_kinesis_provisioned_is_default_mode(tmp_db):
    tf = _tf("aws_kinesis_stream", {"name": "events", "shard_count": 1})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.monthly_cost == pytest.approx(0.015 * 730)


def test_kinesis_on_demand_mode_unsupported(tmp_db):
    tf = _tf("aws_kinesis_stream", {
        "name": "events",
        "stream_mode_details": [{"stream_mode": "ON_DEMAND"}],
    })
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_kinesis_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_kinesis_stream", {"name": "events", "shard_count": 1})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Step Functions ───────────────────────────────────────────────────────────


def test_sfn_standard_is_usage_based(tmp_db):
    tf = _tf("aws_sfn_state_machine", {"name": "workflow"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.name == "State transitions"
    assert comp.price == pytest.approx(2.5e-5 * 1_000)


def test_sfn_express_is_usage_based(tmp_db):
    tf = _tf("aws_sfn_state_machine", {"name": "workflow", "type": "EXPRESS"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    names = [c.name for c in resource.cost_components]
    assert names == ["Requests", "Duration"]


def test_sfn_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_sfn_state_machine", {"name": "workflow"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── EventBridge ──────────────────────────────────────────────────────────────


def test_eventbridge_event_bus_is_usage_based(tmp_db):
    tf = _tf("aws_cloudwatch_event_bus", {"name": "custom-bus"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.price == pytest.approx(1e-6 * 1_000_000)


def test_eventbridge_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_cloudwatch_event_bus", {"name": "custom-bus"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Transit Gateway ──────────────────────────────────────────────────────────


def test_transit_gateway_attachment_flat_hourly_cost(tmp_db):
    tf = _tf("aws_ec2_transit_gateway_vpc_attachment", {"vpc_id": "vpc-1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.05 * 730)
    fixed, data = resource.cost_components
    assert not fixed.usage_based
    assert data.usage_based


def test_transit_gateway_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_ec2_transit_gateway_vpc_attachment", {"vpc_id": "vpc-1"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── S3 Files ─────────────────────────────────────────────────────────────────


def test_s3files_file_system_is_usage_based(tmp_db):
    tf = _tf("aws_s3files_file_system", {"name": "shared-data"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = [c.name for c in resource.cost_components]
    assert names == ["High-performance storage", "Data written to fast tier", "Data read from fast tier"]
    storage, write, read = resource.cost_components
    assert all(c.usage_based for c in (storage, write, read))
    assert storage.price == pytest.approx(0.30)
    assert write.price == pytest.approx(0.06)
    assert read.price == pytest.approx(0.03)


def test_s3files_file_system_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_s3files_file_system", {"name": "shared-data"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_s3files_mount_target_is_free(tmp_db):
    tf = _tf("aws_s3files_mount_target", {"file_system_id": "fs-1", "subnet_id": "subnet-1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0)


def test_s3files_access_point_is_free(tmp_db):
    tf = _tf("aws_s3files_access_point", {"file_system_id": "fs-1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0)


# ── OpenSearch ───────────────────────────────────────────────────────────────


def test_opensearch_domain_instance_and_storage(tmp_db):
    tf = _tf(
        "aws_opensearch_domain",
        {
            "domain_name": "logs",
            "cluster_config": {"instance_type": "r6g.large.elasticsearch", "instance_count": 3},
            "ebs_options": {"ebs_enabled": True, "volume_size": 100, "volume_type": "gp3"},
        },
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    instances, storage = resource.cost_components
    assert not instances.usage_based
    assert not storage.usage_based
    expected = 3 * 0.167 * 730 + 100 * 0.112
    assert resource.monthly_cost == pytest.approx(expected)


def test_opensearch_domain_default_volume_type_is_gp2(tmp_db):
    tf = _tf(
        "aws_opensearch_domain",
        {
            "domain_name": "logs",
            "cluster_config": {"instance_type": "r6g.large.elasticsearch", "instance_count": 1},
            "ebs_options": {"ebs_enabled": True, "volume_size": 50},
        },
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    expected = 1 * 0.167 * 730 + 50 * 0.135
    assert resource.monthly_cost == pytest.approx(expected)


def test_opensearch_domain_unpriced_without_cached_price(empty_db):
    tf = _tf(
        "aws_opensearch_domain",
        {
            "domain_name": "logs",
            "cluster_config": {"instance_type": "r6g.large.elasticsearch", "instance_count": 1},
        },
    )
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_opensearch_domain_missing_instance_type_unpriced(tmp_db):
    tf = _tf("aws_opensearch_domain", {"domain_name": "logs", "cluster_config": {}})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── Redshift ─────────────────────────────────────────────────────────────────


def test_redshift_ra3_cluster_has_usage_based_storage(tmp_db):
    tf = _tf("aws_redshift_cluster", {"cluster_identifier": "warehouse", "node_type": "ra3.xlplus", "number_of_nodes": 2})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(2 * 1.086 * 730)
    compute, storage = resource.cost_components
    assert not compute.usage_based
    assert storage.usage_based
    assert storage.price == pytest.approx(0.024)


def test_redshift_dc2_cluster_has_no_storage_component(tmp_db):
    tf = _tf("aws_redshift_cluster", {"cluster_identifier": "warehouse", "node_type": "dc2.large", "number_of_nodes": 1})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert len(resource.cost_components) == 1
    assert resource.monthly_cost == pytest.approx(1 * 0.25 * 730)


def test_redshift_cluster_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_redshift_cluster", {"cluster_identifier": "warehouse", "node_type": "ra3.xlplus", "number_of_nodes": 1})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_redshift_cluster_missing_node_type_unpriced(tmp_db):
    tf = _tf("aws_redshift_cluster", {"cluster_identifier": "warehouse"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── AWS Backup ───────────────────────────────────────────────────────────────


def test_backup_vault_is_usage_based(tmp_db):
    tf = _tf("aws_backup_vault", {"name": "prod-vault"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = [c.name for c in resource.cost_components]
    assert names == ["Warm storage", "Cold storage", "Restore"]
    assert all(c.usage_based for c in resource.cost_components)


def test_backup_vault_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_backup_vault", {"name": "prod-vault"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_backup_plan_is_free(tmp_db):
    tf = _tf("aws_backup_plan", {"name": "daily"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0)


# ── MSK ──────────────────────────────────────────────────────────────────────


def test_msk_cluster_broker_and_storage(tmp_db):
    tf = _tf(
        "aws_msk_cluster",
        {
            "cluster_name": "events",
            "broker_node_group_info": {
                "instance_type": "kafka.m5.large",
                "number_of_broker_nodes": 3,
                "storage_info": {"ebs_storage_info": {"volume_size": 200}},
            },
        },
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    broker, storage = resource.cost_components
    assert not broker.usage_based
    assert not storage.usage_based
    expected = 3 * 0.21 * 730 + 3 * 200 * 0.10
    assert resource.monthly_cost == pytest.approx(expected)


def test_msk_cluster_without_storage_info_has_no_storage_component(tmp_db):
    tf = _tf(
        "aws_msk_cluster",
        {
            "cluster_name": "events",
            "broker_node_group_info": {"instance_type": "kafka.m5.large", "number_of_broker_nodes": 2},
        },
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert len(resource.cost_components) == 1
    assert resource.monthly_cost == pytest.approx(2 * 0.21 * 730)


def test_msk_cluster_unpriced_without_cached_price(empty_db):
    tf = _tf(
        "aws_msk_cluster",
        {"cluster_name": "events", "broker_node_group_info": {"instance_type": "kafka.m5.large"}},
    )
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_msk_cluster_missing_instance_type_unpriced(tmp_db):
    tf = _tf("aws_msk_cluster", {"cluster_name": "events", "broker_node_group_info": {}})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── Elastic IP ───────────────────────────────────────────────────────────────


def test_eip_flat_monthly_cost(tmp_db):
    tf = _tf("aws_eip", {"domain": "vpc"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.005 * 730)
    [comp] = resource.cost_components
    assert not comp.usage_based


def test_eip_unattached_prices_the_same_as_attached(tmp_db):
    """No attachment field changes the price — that's the whole point of
    the post-Feb-2024 billing model."""
    attached = _tf("aws_eip", {"domain": "vpc", "instance": "i-123"})
    unattached = _tf("aws_eip", {"domain": "vpc"})
    [r1] = price_resources([attached], "us-east-1", db=tmp_db)
    [r2] = price_resources([unattached], "us-east-1", db=tmp_db)
    assert r1.monthly_cost == pytest.approx(r2.monthly_cost)


def test_eip_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_eip", {"domain": "vpc"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── CloudTrail ───────────────────────────────────────────────────────────────


def test_cloudtrail_all_components_usage_based(tmp_db):
    tf = _tf("aws_cloudtrail", {"name": "main"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = [c.name for c in resource.cost_components]
    assert names == [
        "Management events (beyond first free trail)", "Data events", "Insights events",
    ]
    assert all(c.usage_based for c in resource.cost_components)


def test_cloudtrail_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_cloudtrail", {"name": "main"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── GuardDuty ────────────────────────────────────────────────────────────────


def test_guardduty_detector_is_usage_based(tmp_db):
    tf = _tf("aws_guardduty_detector", {"enable": True})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(4.00)


def test_guardduty_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_guardduty_detector", {"enable": True})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── DocumentDB ───────────────────────────────────────────────────────────────


def test_docdb_cluster_instance_flat_monthly_cost(tmp_db):
    tf = _tf("aws_docdb_cluster_instance", {"identifier": "db-1", "instance_class": "db.r5.large"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.277 * 730)


def test_docdb_cluster_instance_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_docdb_cluster_instance", {"identifier": "db-1", "instance_class": "db.r5.large"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_docdb_cluster_instance_missing_instance_class_unpriced(tmp_db):
    tf = _tf("aws_docdb_cluster_instance", {"identifier": "db-1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── FSx for Windows ──────────────────────────────────────────────────────────


def test_fsx_windows_ssd_storage_and_throughput(tmp_db):
    tf = _tf(
        "aws_fsx_windows_file_system",
        {"storage_capacity": 300, "throughput_capacity": 16},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    storage, throughput = resource.cost_components
    assert not storage.usage_based
    assert not throughput.usage_based
    expected = 300 * 0.13 + 16 * 2.20
    assert resource.monthly_cost == pytest.approx(expected)


def test_fsx_windows_hdd_storage(tmp_db):
    tf = _tf(
        "aws_fsx_windows_file_system",
        {"storage_capacity": 2000, "storage_type": "HDD", "throughput_capacity": 8},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    expected = 2000 * 0.013 + 8 * 2.20
    assert resource.monthly_cost == pytest.approx(expected)


def test_fsx_windows_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_fsx_windows_file_system", {"storage_capacity": 300, "throughput_capacity": 16})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_fsx_windows_missing_throughput_capacity_unpriced(tmp_db):
    tf = _tf("aws_fsx_windows_file_system", {"storage_capacity": 300})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


# ── ACM Private CA ───────────────────────────────────────────────────────────


def test_acmpca_general_purpose_default(tmp_db):
    tf = _tf("aws_acmpca_certificate_authority", {"type": "ROOT"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(400.00)
    flat, issued = resource.cost_components
    assert not flat.usage_based
    assert issued.usage_based
    assert issued.price == pytest.approx(0.75)


def test_acmpca_short_lived(tmp_db):
    tf = _tf("aws_acmpca_certificate_authority", {"type": "ROOT", "usage_mode": "SHORT_LIVED_CERTIFICATE"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(50.00)


def test_acmpca_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_acmpca_certificate_authority", {"type": "ROOT"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Athena ───────────────────────────────────────────────────────────────────


def test_athena_workgroup_is_usage_based(tmp_db):
    tf = _tf("aws_athena_workgroup", {"name": "primary"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(5.00)


def test_athena_workgroup_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_athena_workgroup", {"name": "primary"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── FSx for Lustre ───────────────────────────────────────────────────────────


def test_fsx_lustre_storage_by_deployment_and_type(tmp_db):
    tf = _tf(
        "aws_fsx_lustre_file_system",
        {"storage_capacity": 1200, "deployment_type": "SCRATCH_2"},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(1200 * 0.14)


def test_fsx_lustre_persistent_hdd(tmp_db):
    tf = _tf(
        "aws_fsx_lustre_file_system",
        {"storage_capacity": 5000, "deployment_type": "PERSISTENT_1", "storage_type": "HDD"},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(5000 * 0.025)


def test_fsx_lustre_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_fsx_lustre_file_system", {"storage_capacity": 1200, "deployment_type": "SCRATCH_2"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Neptune ──────────────────────────────────────────────────────────────────


def test_neptune_cluster_instance_flat_monthly_cost(tmp_db):
    tf = _tf("aws_neptune_cluster_instance", {"identifier": "db-1", "instance_class": "db.r5.large"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.348 * 730)


def test_neptune_cluster_instance_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_neptune_cluster_instance", {"identifier": "db-1", "instance_class": "db.r5.large"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Global Accelerator ───────────────────────────────────────────────────────


def test_global_accelerator_flat_plus_usage_based_data(tmp_db):
    tf = _tf("aws_globalaccelerator_accelerator", {"name": "accel"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.025 * 730)
    fixed, data = resource.cost_components
    assert not fixed.usage_based
    assert data.usage_based
    assert data.price == pytest.approx(0.015)


def test_global_accelerator_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_globalaccelerator_accelerator", {"name": "accel"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


# ── Amazon MQ ────────────────────────────────────────────────────────────────


def test_mq_broker_single_instance(tmp_db):
    tf = _tf("aws_mq_broker", {"broker_name": "orders", "host_instance_type": "mq.m5.large"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(1 * 0.30 * 730)
    broker, storage = resource.cost_components
    assert not broker.usage_based
    assert storage.usage_based


def test_mq_broker_active_standby_doubles_broker_count(tmp_db):
    tf = _tf(
        "aws_mq_broker",
        {"broker_name": "orders", "host_instance_type": "mq.m5.large", "deployment_mode": "ACTIVE_STANDBY_MULTI_AZ"},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(2 * 0.30 * 730)


def test_mq_broker_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_mq_broker", {"broker_name": "orders", "host_instance_type": "mq.m5.large"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_vpn_connection_flat_hourly(tmp_db):
    tf = _tf("aws_vpn_connection", {"customer_gateway_id": "cgw-1", "type": "ipsec.1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.05 * 730)


def test_vpn_connection_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_vpn_connection", {"customer_gateway_id": "cgw-1", "type": "ipsec.1"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_client_vpn_endpoint_fully_usage_based(tmp_db):
    tf = _tf("aws_ec2_client_vpn_endpoint", {"description": "corp-vpn"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = {c.name for c in resource.cost_components}
    assert names == {"Subnet associations", "Active connections"}
    assert all(c.usage_based for c in resource.cost_components)


def test_dx_connection_by_bandwidth(tmp_db):
    tf = _tf("aws_dx_connection", {"name": "corp-dx", "bandwidth": "1Gbps"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.30 * 730)


def test_dx_hosted_connection_by_bandwidth(tmp_db):
    tf = _tf("aws_dx_hosted_connection", {"name": "corp-dx", "bandwidth": "10Gbps"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(2.25 * 730)


def test_dx_connection_unpriced_missing_bandwidth(tmp_db):
    tf = _tf("aws_dx_connection", {"name": "corp-dx"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_appsync_graphql_api_fully_usage_based(tmp_db):
    tf = _tf("aws_appsync_graphql_api", {"name": "orders-api"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = {c.name for c in resource.cost_components}
    assert names == {"Query and data modification operations", "Real-time subscription connection-minutes"}
    assert all(c.usage_based for c in resource.cost_components)
    req_comp = next(c for c in resource.cost_components if c.name == "Query and data modification operations")
    assert req_comp.price == pytest.approx(4.0 * 1_000_000)


def test_cognito_user_pool_fully_usage_based(tmp_db):
    tf = _tf("aws_cognito_user_pool", {"name": "users"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.name == "Monthly active users"
    assert comp.usage_based
    assert comp.price == pytest.approx(0.0055)


def test_cognito_user_pool_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_cognito_user_pool", {"name": "users"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_glue_job_fully_usage_based(tmp_db):
    tf = _tf("aws_glue_job", {"name": "etl-job", "max_capacity": 10})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.name == "DPU-hours"
    assert comp.usage_based
    assert comp.price == pytest.approx(0.44)


def test_glue_crawler_fully_usage_based(tmp_db):
    tf = _tf("aws_glue_crawler", {"name": "catalog-crawler"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based
    assert comp.price == pytest.approx(0.44)


def test_glue_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_glue_job", {"name": "etl-job"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_sagemaker_notebook_instance_flat_hourly(tmp_db):
    tf = _tf("aws_sagemaker_notebook_instance", {"name": "notebook", "instance_type": "ml.t3.medium"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0582 * 730)


def test_sagemaker_notebook_instance_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_sagemaker_notebook_instance", {"name": "notebook", "instance_type": "ml.t3.medium"})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_sagemaker_endpoint_configuration_single_variant(tmp_db):
    tf = _tf(
        "aws_sagemaker_endpoint_configuration",
        {"name": "orders-endpoint", "production_variants": [
            {"variant_name": "primary", "instance_type": "ml.m5.xlarge", "initial_instance_count": 2},
        ]},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(2 * 0.269 * 730)


def test_sagemaker_endpoint_configuration_bare_dict_variant(tmp_db):
    tf = _tf(
        "aws_sagemaker_endpoint_configuration",
        {"name": "orders-endpoint", "production_variants": {
            "variant_name": "primary", "instance_type": "ml.t3.medium", "initial_instance_count": 1,
        }},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0582 * 730)


def test_sagemaker_endpoint_configuration_serverless_variant_unpriced_component(tmp_db):
    tf = _tf(
        "aws_sagemaker_endpoint_configuration",
        {"name": "orders-endpoint", "production_variants": [
            {"variant_name": "serverless", "serverless_config": {"max_concurrency": 5}},
        ]},
    )
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_cloudhsm_hsm_flat_hourly(tmp_db):
    tf = _tf("aws_cloudhsm_v2_hsm", {"cluster_id": "cluster-1"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(1.60 * 730)


def test_cloudhsm_cluster_free(tmp_db):
    tf = _tf("aws_cloudhsm_v2_cluster", {"hsm_type": "hsm1.medium"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost == pytest.approx(0.0)


def test_macie_account_fully_usage_based(tmp_db):
    tf = _tf("aws_macie2_account", {})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.name == "Data evaluated (S3)"
    assert comp.usage_based
    assert comp.price == pytest.approx(1.00)


def test_macie_classification_job_fully_usage_based(tmp_db):
    tf = _tf("aws_macie2_classification_job", {"name": "job1", "job_type": "ONE_TIME"})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    [comp] = resource.cost_components
    assert comp.usage_based


def test_macie_unpriced_without_cached_price(empty_db):
    tf = _tf("aws_macie2_account", {})
    [resource] = price_resources([tf], "us-east-1", db=empty_db)
    assert resource.no_price


def test_inspector_enabler_multiple_resource_types(tmp_db):
    tf = _tf("aws_inspector2_enabler", {"account_ids": ["123456789012"], "resource_types": ["EC2", "ECR", "LAMBDA"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.is_supported
    assert resource.monthly_cost is None
    names = {c.name for c in resource.cost_components}
    assert names == {"EC2 instance scanning", "ECR image scanning", "Lambda function scanning"}
    assert all(c.usage_based for c in resource.cost_components)


def test_inspector_enabler_unpriced_without_matching_resource_type(tmp_db):
    tf = _tf("aws_inspector2_enabler", {"account_ids": ["123456789012"], "resource_types": ["UNKNOWN"]})
    [resource] = price_resources([tf], "us-east-1", db=tmp_db)
    assert resource.no_price


def test_unsupported_resource_type_skipped(tmp_db):
    tf = _tf("aws_iam_role", {})
    assert price_resources([tf], "us-east-1", db=tmp_db) == []


def test_build_output_totals(tmp_db):
    tf_ec2 = _tf("aws_instance", {"instance_type": "t3.micro"})
    tf_bad = _tf("aws_instance", {})
    resources = price_resources([tf_ec2, tf_bad], "us-east-1", db=tmp_db)
    output = build_output(resources, "us-east-1")
    assert output.total_monthly_cost == pytest.approx(0.0104 * 730)
    assert output.summary["totalDetectedResources"] == 2
    assert output.summary["totalSupportedResources"] == 1
    assert output.summary["totalNoPriceResources"] == 1
    assert len(output.projects) == 1
    assert output.projects[0].breakdown.resources == resources


# ── Data Transfer ────────────────────────────────────────────────────────────


def test_data_transfer_all_components_unit_priced_no_total(tmp_db):
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:0", "GB", 0.09, db=tmp_db)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:10240", "GB", 0.085, db=tmp_db)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:regional", "GB", 0.01, db=tmp_db)

    resource = price_data_transfer("us-east-1", db=tmp_db)
    assert resource is not None
    assert resource.monthly_cost is None
    assert resource.total_monthly_cost() == pytest.approx(0.0)
    names = [c.name for c in resource.cost_components]
    assert names == [
        "Internet egress, first tier",
        "Internet egress, above 10,240 GB/mo",
        "Inter-AZ transfer",
    ]
    assert all(c.usage_based and c.monthly_cost is None for c in resource.cost_components)


def test_data_transfer_tiers_sorted_regardless_of_insertion_order(tmp_db):
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:153600", "GB", 0.05, db=tmp_db)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:0", "GB", 0.09, db=tmp_db)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:out:51200", "GB", 0.07, db=tmp_db)

    resource = price_data_transfer("us-east-1", db=tmp_db)
    prices = [c.price for c in resource.cost_components]
    assert prices == pytest.approx([0.09, 0.07, 0.05])


def test_data_transfer_none_without_cached_price(empty_db):
    assert price_data_transfer("us-east-1", db=empty_db) is None


_DT_TIERS = [
    ("datatransfer:out:0", 0.09),
    ("datatransfer:out:10240", 0.085),
    ("datatransfer:out:51200", 0.07),
    ("datatransfer:out:153600", 0.05),
]


def _seed_data_transfer_tiers(db):
    for key, price in _DT_TIERS:
        price_db.upsert("AWSDataTransfer", "us-east-1", key, "GB", price, db=db)
    price_db.upsert("AWSDataTransfer", "us-east-1", "datatransfer:regional", "GB", 0.01, db=db)


def test_estimate_data_transfer_within_first_tier(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    est = estimate_data_transfer_cost(
        "us-east-1", {"data_transfer": {"internet_egress_gb_month": 5000}}, db=tmp_db,
    )
    assert est == pytest.approx(5000 * 0.09)


def test_estimate_data_transfer_spans_multiple_tiers(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    # 15,000 GB = 10,240 GB at tier 0 + 4,760 GB at tier 1.
    est = estimate_data_transfer_cost(
        "us-east-1", {"data_transfer": {"internet_egress_gb_month": 15000}}, db=tmp_db,
    )
    assert est == pytest.approx(10240 * 0.09 + 4760 * 0.085)


def test_estimate_data_transfer_reaches_top_uncapped_tier(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    total_gb = 200_000
    est = estimate_data_transfer_cost(
        "us-east-1", {"data_transfer": {"internet_egress_gb_month": total_gb}}, db=tmp_db,
    )
    expected = (
        10240 * 0.09
        + (51200 - 10240) * 0.085
        + (153600 - 51200) * 0.07
        + (total_gb - 153600) * 0.05
    )
    assert est == pytest.approx(expected)


def test_estimate_data_transfer_includes_inter_az(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    est = estimate_data_transfer_cost(
        "us-east-1",
        {"data_transfer": {"internet_egress_gb_month": 1000, "inter_az_gb_month": 200}},
        db=tmp_db,
    )
    assert est == pytest.approx(1000 * 0.09 + 200 * 0.01)


def test_estimate_data_transfer_inter_az_only(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    est = estimate_data_transfer_cost(
        "us-east-1", {"data_transfer": {"inter_az_gb_month": 200}}, db=tmp_db,
    )
    assert est == pytest.approx(200 * 0.01)


def test_estimate_data_transfer_none_without_usage_file_data(tmp_db):
    _seed_data_transfer_tiers(tmp_db)
    assert estimate_data_transfer_cost("us-east-1", {}, db=tmp_db) is None


def test_estimate_data_transfer_none_without_cached_price(empty_db):
    est = estimate_data_transfer_cost(
        "us-east-1", {"data_transfer": {"internet_egress_gb_month": 1000}}, db=empty_db,
    )
    assert est is None


# ── Cost Explorer actuals for the new usage categories ──────────────────────


def test_estimate_s3_storage_cost_single_bucket(tmp_db):
    tf = _tf("aws_s3_bucket", {})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    estimates = estimate_s3_storage_cost(resources, {"storage_gb": 1000})
    assert estimates == {"aws_s3_bucket.thing": pytest.approx(1000 * 0.023)}


def test_estimate_s3_storage_cost_splits_across_buckets(tmp_db):
    tfs = [_tf("aws_s3_bucket", {}, address="aws_s3_bucket.a"), _tf("aws_s3_bucket", {}, address="aws_s3_bucket.b")]
    resources = price_resources(tfs, "us-east-1", db=tmp_db)
    estimates = estimate_s3_storage_cost(resources, {"storage_gb": 1000})
    assert estimates == {
        "aws_s3_bucket.a": pytest.approx(500 * 0.023),
        "aws_s3_bucket.b": pytest.approx(500 * 0.023),
    }


def test_estimate_s3_storage_cost_empty_without_usage(tmp_db):
    tf = _tf("aws_s3_bucket", {})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    assert estimate_s3_storage_cost(resources, {}) == {}


def test_estimate_elb_lcu_cost(tmp_db):
    tf = _tf("aws_lb", {"load_balancer_type": "application"})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    estimates = estimate_elb_lcu_cost(resources, {"lcu_hours_month": 100})
    assert estimates == {"aws_lb.thing": pytest.approx(100 * 0.008)}


def test_estimate_elb_lcu_cost_skips_classic_data_processed_component(tmp_db):
    """Classic ELB's usage-based component bills GB, not LCUs — shouldn't match."""
    tf = _tf("aws_elb", {})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    assert estimate_elb_lcu_cost(resources, {"lcu_hours_month": 100}) == {}


def test_estimate_rds_storage_cost_aurora_only(tmp_db):
    price_db.upsert("AmazonRDS", "us-east-1", "rds:db.r5.large:Aurora PostgreSQL:Single-AZ", "Hrs", 0.29, db=tmp_db)
    aurora = _tf("aws_db_instance", {"instance_class": "db.r5.large", "engine": "aurora-postgresql"}, address="aws_db_instance.aurora")
    standard = _tf("aws_db_instance", {"instance_class": "db.t3.medium", "engine": "postgres"}, address="aws_db_instance.standard")
    resources = price_resources([aurora, standard], "us-east-1", db=tmp_db)
    estimates = estimate_rds_storage_cost(resources, {"storage_gb": 100})
    assert estimates == {"aws_db_instance.aurora": pytest.approx(100 * 0.10)}  # fallback rate, no cached price


def test_apply_ec2_runtime_actuals_overrides_flat_730h(tmp_db):
    tf = _tf("aws_instance", {"instance_type": "t3.micro"})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    updated = apply_ec2_runtime_actuals(resources, 200)
    assert updated == ["aws_instance.thing"]
    assert resources[0].monthly_cost == pytest.approx(0.0104 * 200)


def test_apply_ec2_runtime_actuals_splits_across_instances(tmp_db):
    tfs = [_tf("aws_instance", {"instance_type": "t3.micro"}, address="aws_instance.a"),
           _tf("aws_instance", {"instance_type": "t3.micro"}, address="aws_instance.b")]
    resources = price_resources(tfs, "us-east-1", db=tmp_db)
    apply_ec2_runtime_actuals(resources, 200)
    assert resources[0].monthly_cost == pytest.approx(0.0104 * 100)
    assert resources[1].monthly_cost == pytest.approx(0.0104 * 100)


def test_apply_elasticache_runtime_actuals_overrides_flat_730h(tmp_db):
    tf = _tf("aws_elasticache_cluster", {"node_type": "cache.t3.micro", "engine": "redis"})
    resources = price_resources([tf], "us-east-1", db=tmp_db)
    updated = apply_elasticache_runtime_actuals(resources, 300)
    assert updated == ["aws_elasticache_cluster.thing"]
    assert resources[0].monthly_cost == pytest.approx(0.017 * 300)


def _resource(name, no_price=False, unsupported=False, resource_type="aws_instance", monthly_cost=10.0):
    return Resource(
        name=name,
        resource_type=resource_type,
        tags={},
        monthly_cost=None if (no_price or unsupported) else monthly_cost,
        hourly_cost=None,
        cost_components=[],
        sub_resources=[],
        is_supported=not (no_price or unsupported),
        no_price=no_price,
    )


def test_build_multi_project_output_aggregates_top_level_summary():
    """Regression: build_multi_project_output used to hardcode summary={} at
    the top level even though it computed correct per-project summaries --
    the report's Estimated/Free/Detected/Unsupported resource cards read only
    the top-level InfracostOutput.summary, so they always showed 0."""
    resources_by_project = {
        "stack-a": [_resource("a1"), _resource("a2", no_price=True)],
        "stack-b": [_resource("b1", unsupported=True)],
    }
    output = build_multi_project_output(resources_by_project)

    assert output.summary["totalDetectedResources"] == 3
    assert output.summary["totalSupportedResources"] == 1
    assert output.summary["totalNoPriceResources"] == 1
    assert output.summary["totalUnsupportedResources"] == 1


def test_price_terraform_json_routes_raw_multi_stack():
    data = {"stack-a": {"resources": []}}
    output = price_terraform_json(data, "us-east-1")
    assert output.version == "bucksawz-price-state-multi-1"
    assert output.projects[0].name == "stack-a"


def test_find_new_resources_returns_only_undeployed():
    actual = build_multi_project_output({
        "stack-a": [_resource("aws_instance.a1")],
    })
    proposed = build_multi_project_output({
        "stack-a": [_resource("aws_instance.a1"), _resource("aws_instance.a2")],
    })
    new = find_new_resources(actual, proposed)
    assert list(new.keys()) == ["stack-a"]
    assert [r.name for r in new["stack-a"]] == ["aws_instance.a2"]


def test_find_new_resources_whole_new_project_counts_all_resources():
    actual = build_multi_project_output({"stack-a": [_resource("aws_instance.a1")]})
    proposed = build_multi_project_output({
        "stack-a": [_resource("aws_instance.a1")],
        "stack-b": [_resource("aws_instance.b1")],
    })
    new = find_new_resources(actual, proposed)
    assert [r.name for r in new["stack-b"]] == ["aws_instance.b1"]


def test_find_new_resources_no_diff_is_empty():
    actual = build_multi_project_output({"stack-a": [_resource("aws_instance.a1")]})
    proposed = build_multi_project_output({"stack-a": [_resource("aws_instance.a1")]})
    assert find_new_resources(actual, proposed) == {}


def test_extrapolate_by_resource_type_averages_deployed_same_type():
    actual = build_multi_project_output({
        "stack-a": [
            _resource("aws_instance.a1", resource_type="aws_instance", monthly_cost=10.0),
            _resource("aws_instance.a2", resource_type="aws_instance", monthly_cost=20.0),
        ],
    })
    new_by_project = {"stack-a": [_resource("aws_instance.a3", resource_type="aws_instance")]}
    estimates = extrapolate_by_resource_type(actual, new_by_project)
    assert estimates["stack-a"]["aws_instance.a3"] == pytest.approx(15.0)


def test_extrapolate_by_resource_type_no_comparable_type_is_none():
    actual = build_multi_project_output({
        "stack-a": [_resource("aws_instance.a1", resource_type="aws_instance")],
    })
    new_by_project = {"stack-a": [_resource("aws_s3_bucket.new", resource_type="aws_s3_bucket")]}
    estimates = extrapolate_by_resource_type(actual, new_by_project)
    assert estimates["stack-a"]["aws_s3_bucket.new"] is None


def test_extrapolate_by_resource_type_excludes_unsupported_and_no_price():
    actual = build_multi_project_output({
        "stack-a": [
            _resource("aws_instance.a1", resource_type="aws_instance", monthly_cost=10.0),
            _resource("aws_instance.a2", resource_type="aws_instance", unsupported=True),
            _resource("aws_instance.a3", resource_type="aws_instance", no_price=True),
        ],
    })
    new_by_project = {"stack-a": [_resource("aws_instance.a4", resource_type="aws_instance")]}
    estimates = extrapolate_by_resource_type(actual, new_by_project)
    # Only a1 (the sole supported, priced instance) contributes to the average.
    assert estimates["stack-a"]["aws_instance.a4"] == pytest.approx(10.0)
