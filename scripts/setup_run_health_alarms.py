"""
One-time setup script: create CloudWatch alarms for ACP Run-Health (AA-141).

Alarms:
  a. StuckRunAlarm        — acp/stuck_runs > 0 for 5 min
  b. CostCapAlarm         — acp/cost_usd_per_run > 10
  c. EvaluatorScoreAlarm  — acp/evaluator_score < 7.0
  d. GateSLABreachAlarm   — acp/gate_sla_breached > 0

Metrics are emitted by GET /admin/acp/run-health on every query.

Usage:
  SNS_TOPIC_ARN=arn:aws:sns:us-west-1:<account>:aa-cis-dev-alerts \
  AWS_PROFILE=pqnghiep-admin python3 scripts/setup_run_health_alarms.py
"""
import os
import sys
import boto3

REGION = "us-west-1"
NAMESPACE = "acp"
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

if not SNS_TOPIC_ARN:
    print("ERROR: SNS_TOPIC_ARN env var required", file=sys.stderr)
    sys.exit(1)

cw = boto3.client("cloudwatch", region_name=REGION)

alarms = [
    {
        "AlarmName": "aa-cis-dev-acp-stuck-runs",
        "AlarmDescription": "One or more ACP runs stuck beyond stage SLO duration",
        "Namespace": NAMESPACE,
        "MetricName": "stuck_runs",
        "Statistic": "Sum",
        "Period": 300,
        "EvaluationPeriods": 1,
        "Threshold": 1.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData": "notBreaching",
        "AlarmActions": [SNS_TOPIC_ARN],
        "OKActions": [SNS_TOPIC_ARN],
    },
    {
        "AlarmName": "aa-cis-dev-acp-cost-cap",
        "AlarmDescription": "ACP run LLM cost exceeded $10 cap",
        "Namespace": NAMESPACE,
        "MetricName": "cost_usd_per_run",
        "Statistic": "Maximum",
        "Period": 300,
        "EvaluationPeriods": 1,
        "Threshold": 10.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching",
        "AlarmActions": [SNS_TOPIC_ARN],
        "OKActions": [SNS_TOPIC_ARN],
    },
    {
        "AlarmName": "aa-cis-dev-acp-evaluator-score-low",
        "AlarmDescription": "ACP evaluator score below 7.0 floor",
        "Namespace": NAMESPACE,
        "MetricName": "evaluator_score",
        "Statistic": "Average",
        "Period": 300,
        "EvaluationPeriods": 1,
        "Threshold": 7.0,
        "ComparisonOperator": "LessThanThreshold",
        "TreatMissingData": "notBreaching",
        "AlarmActions": [SNS_TOPIC_ARN],
        "OKActions": [SNS_TOPIC_ARN],
    },
    {
        "AlarmName": "aa-cis-dev-acp-gate-sla-breach",
        "AlarmDescription": "ACP gate SLA breached — pending HITL past deadline",
        "Namespace": NAMESPACE,
        "MetricName": "gate_sla_breached",
        "Statistic": "Sum",
        "Period": 300,
        "EvaluationPeriods": 1,
        "Threshold": 1.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData": "notBreaching",
        "AlarmActions": [SNS_TOPIC_ARN],
        "OKActions": [SNS_TOPIC_ARN],
    },
]

for alarm in alarms:
    cw.put_metric_alarm(**alarm)
    print(f"Created alarm: {alarm['AlarmName']}")

print("Done — 4 CloudWatch alarms configured.")
