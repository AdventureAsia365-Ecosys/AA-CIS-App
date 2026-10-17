# AA-CIS Operations Runbook
## Adventure Asia Content Intelligence System

**Version:** 1.0  
**Last Updated:** 20/04/2026  
**Author:** Pham Quoc Nghiep  
**Environment:** us-west-1 · Account: 867490540162  
**API URL:** https://api-cis.lumiguides.it.com

---

## Prerequisites

```bash
# Always verify account before any operation
aws sts get-caller-identity --profile pqnghiep-admin
# Expected: Account: 867490540162

# Key variables
export AWS_REGION=us-west-1
export AWS_PROFILE=pqnghiep-admin
export CLUSTER=aa-cis-dev-cluster
export SERVICE=aa-cis-dev-api
export ECR=867490540162.dkr.ecr.us-west-1.amazonaws.com/aa-cis-dev-api
```

---

## 1. Deploy New Version

### 1.1 Build and push Docker image

```bash
cd ~/projects/aa-cis/AA-CIS-App

# Login ECR
aws ecr get-login-password \
  --region us-west-1 \
  --profile pqnghiep-admin | \
docker login --username AWS \
  --password-stdin $ECR

# Build
docker build -t aa-cis-dev-api .

# Tag + Push (AA-869: the ECR repo is IMMUTABLE; push only the per-commit dev-<full sha> tag,
# never :latest. Normal deploys go through the Deploy Dev workflow, not this manual path.)
docker tag aa-cis-dev-api:latest $ECR:dev-$(git rev-parse HEAD)
docker push $ECR:dev-$(git rev-parse HEAD)
```

### 1.2 Force ECS redeploy

```bash
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE \
  --force-new-deployment \
  --region us-west-1 \
  --profile pqnghiep-admin
```

### 1.3 Monitor deployment

```bash
# Watch until Running=1, Pending=0
watch -n 10 "aws ecs describe-services \
  --cluster $CLUSTER --services $SERVICE \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'services[0].{Running:runningCount,Pending:pendingCount,Event:events[0].message}'"
```

### 1.4 Verify health

```bash
curl https://api-cis.lumiguides.it.com/health
# Expected: {"status":"ok","service":"aa-cis-api"}
```

**Estimated time:** ~5 minutes

---

## 2. Rollback

### 2.1 List recent task definitions

```bash
aws ecs list-task-definitions \
  --family-prefix aa-cis-dev-api \
  --sort DESC \
  --region us-west-1 \
  --profile pqnghiep-admin \
  --query 'taskDefinitionArns[:5]'
```

### 2.2 Rollback to previous revision

```bash
# Example: rollback to revision 2
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE \
  --task-definition aa-cis-dev-api:2 \
  --region us-west-1 \
  --profile pqnghiep-admin
```

### 2.3 Verify rollback

```bash
aws ecs describe-services \
  --cluster $CLUSTER --services $SERVICE \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'services[0].{TaskDef:taskDefinition,Running:runningCount}'

curl https://api-cis.lumiguides.it.com/health
```

**Estimated time:** ~3 minutes

---

## 3. Requeue Dead Letter Queue (DLQ)

### 3.1 Check DLQ message counts

```bash
for queue in ingestion seo content-gen validation export hitl; do
  echo -n "$queue DLQ: "
  aws sqs get-queue-attributes \
    --queue-url "https://sqs.us-west-1.amazonaws.com/867490540162/aa-cis-dev-${queue}-dlq" \
    --attribute-names ApproximateNumberOfMessages \
    --region us-west-1 --profile pqnghiep-admin \
    --query 'Attributes.ApproximateNumberOfMessages' \
    --output text
done
```

### 3.2 Redrive messages from DLQ back to main queue

```bash
# Example: requeue ingestion DLQ
SOURCE_DLQ="https://sqs.us-west-1.amazonaws.com/867490540162/aa-cis-dev-ingestion-dlq"
TARGET_QUEUE="https://sqs.us-west-1.amazonaws.com/867490540162/aa-cis-dev-ingestion"

# Get messages from DLQ
MESSAGES=$(aws sqs receive-message \
  --queue-url $SOURCE_DLQ \
  --max-number-of-messages 10 \
  --region us-west-1 --profile pqnghiep-admin)

# For each message: send to main queue + delete from DLQ
echo $MESSAGES | python3 -c "
import sys, json, boto3

sqs = boto3.client('sqs', region_name='us-west-1')
data = json.load(sys.stdin)
messages = data.get('Messages', [])
print(f'Requeuing {len(messages)} messages...')

for msg in messages:
    # Send to main queue
    sqs.send_message(
        QueueUrl='$TARGET_QUEUE',
        MessageBody=msg['Body']
    )
    # Delete from DLQ
    sqs.delete_message(
        QueueUrl='$SOURCE_DLQ',
        ReceiptHandle=msg['ReceiptHandle']
    )
    print(f'  Requeued: {msg[\"MessageId\"]}')

print('Done.')
"
```

### 3.3 Verify DLQ cleared

```bash
aws sqs get-queue-attributes \
  --queue-url "https://sqs.us-west-1.amazonaws.com/867490540162/aa-cis-dev-ingestion-dlq" \
  --attribute-names ApproximateNumberOfMessages \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'Attributes.ApproximateNumberOfMessages'
# Expected: "0"
```

---

## 4. Add New Brand Validator Rule

### 4.1 Add validator function

```python
# File: shared/validators/rules.py
# Add new function following existing pattern

def v30_no_passive_voice(content: str) -> ValidationResult:
    """
    v30: Avoid passive voice — use active voice for engagement.
    Bad:  'The tour is led by an experienced guide.'
    Good: 'An experienced guide leads the tour.'
    """
    passive_patterns = [
        r'\b(is|are|was|were|be|been|being)\s+\w+ed\b',
    ]
    import re
    for pattern in passive_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            return ValidationResult(
                passed=False,
                failure_code="V30_PASSIVE_VOICE",
                message="Avoid passive voice constructions."
            )
    return ValidationResult(passed=True)
```

### 4.2 Register in lessons_registry

```bash
psql "$DATABASE_URL" << 'SQL'
INSERT INTO shared.lessons_registry
    (lesson_num, category, validator_fn, failure_code,
     example_before, example_after, is_active)
VALUES (
    'v30',
    'style',
    'v30_no_passive_voice',
    'V30_PASSIVE_VOICE',
    'The tour is led by an experienced guide.',
    'An experienced guide leads the tour.',
    TRUE
);
SQL
```

### 4.3 Add unit test

```python
# File: tests/unit/test_validators.py
def test_v30_passive_voice_rejected():
    result = v30_no_passive_voice("The tour is led by a guide.")
    assert result.passed is False
    assert result.failure_code == "V30_PASSIVE_VOICE"

def test_v30_active_voice_passes():
    result = v30_no_passive_voice("An experienced guide leads the tour.")
    assert result.passed is True
```

### 4.4 Run regression tests

```bash
cd ~/projects/aa-cis/AA-CIS-App
pytest tests/integration/ -v
# Must be: 104/104 passed (+ new tests)
```

**Estimated time:** ~30 minutes

---

## 5. Scale ECS Service

### 5.1 Scale up (increase capacity)

```bash
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE \
  --desired-count 3 \
  --region us-west-1 \
  --profile pqnghiep-admin
```

### 5.2 Scale down to 0 (cost saving after dev session)

```bash
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE \
  --desired-count 0 \
  --region us-west-1 \
  --profile pqnghiep-admin

echo "ECS scaled to 0. Remember to scale back up before next session."
```

### 5.3 Scale back up

```bash
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE \
  --desired-count 1 \
  --region us-west-1 \
  --profile pqnghiep-admin

# Wait for healthy
aws ecs wait services-stable \
  --cluster $CLUSTER \
  --services $SERVICE \
  --region us-west-1 \
  --profile pqnghiep-admin

curl https://api-cis.lumiguides.it.com/health
```

---

## 6. Check Logs

### 6.1 Live log tail

```bash
aws logs tail /ecs/aa-cis-dev \
  --follow \
  --since 30m \
  --region us-west-1 \
  --profile pqnghiep-admin
```

### 6.2 Filter for errors

```bash
aws logs filter-log-events \
  --log-group-name /ecs/aa-cis-dev \
  --filter-pattern "ERROR" \
  --start-time $(date -d '1 hour ago' +%s000) \
  --region us-west-1 \
  --profile pqnghiep-admin \
  --query 'events[].message'
```

### 6.3 Get last N log lines

```bash
STREAM=$(aws logs describe-log-streams \
  --log-group-name /ecs/aa-cis-dev \
  --order-by LastEventTime --descending \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'logStreams[0].logStreamName' --output text)

aws logs get-log-events \
  --log-group-name /ecs/aa-cis-dev \
  --log-stream-name $STREAM \
  --limit 50 \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'events[].message'
```

---

## 7. Apply Database Migration

### 7.1 Via local psql (VPN/tunnel required for RDS)

```bash
# RDS is in private subnet — needs SSM port forward or bastion
# For local test DB:
PGPASSWORD=cistest psql -h 127.0.0.1 -U cistest -d cis_integration_test \
  -f migrations/007_new_migration.sql
```

### 7.2 Via ECS task (for prod RDS)

```bash
# Run migration as one-off ECS task
aws ecs run-task \
  --cluster $CLUSTER \
  --task-definition aa-cis-dev-api \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","scripts.migrate","007"]}]}' \
  --region us-west-1 \
  --profile pqnghiep-admin
```

---

## 8. Cost Management

### 8.1 Check current month cost

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date +%Y-%m-01),End=$(date +%Y-%m-%d) \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --profile pqnghiep-admin \
  --query 'ResultsByTime[0].Groups[*].{Service:Keys[0],Cost:Metrics.BlendedCost.Amount}' \
  --output table
```

### 8.2 Stop all resources after dev session

```bash
# 1. Scale ECS to 0
aws ecs update-service \
  --cluster $CLUSTER --service $SERVICE \
  --desired-count 0 \
  --region us-west-1 --profile pqnghiep-admin

# 2. RDS is already stopped (cleanup done 20/04)
# To stop RDS if started:
# aws rds stop-db-instance \
#   --db-instance-identifier aa-cis-dev-db \
#   --region us-west-1 --profile pqnghiep-admin

echo "Resources stopped. NAT Gateway still running (~\$1.08/day)"
echo "To save more: terraform destroy -target module.vpc.aws_nat_gateway.main"
```

### 8.3 Cost targets

| Resource | Current/month | Target |
|----------|--------------|--------|
| RDS PostgreSQL 16 | ~$23 | ✅ |
| ALB | ~$15 | ✅ |
| NAT Gateway | ~$13 | ⚠️ High when idle |
| ElastiCache | ~$6 | ✅ |
| ECS Fargate | ~$2-10 | ✅ |
| **Total** | **~$73** | **✅ < $106** |

---

## 9. Alarms & Monitoring

### 9.1 Check CloudWatch alarms

```bash
aws cloudwatch describe-alarms \
  --region us-west-1 \
  --profile pqnghiep-admin \
  --query 'MetricAlarms[*].{Name:AlarmName,State:StateValue}' \
  --output table
```

### 9.2 ECS service health

```bash
aws ecs describe-services \
  --cluster $CLUSTER --services $SERVICE \
  --region us-west-1 --profile pqnghiep-admin \
  --query 'services[0].{Status:status,Running:runningCount,Desired:desiredCount,TaskDef:taskDefinition}'
```

### 9.3 Target group health

```bash
aws elbv2 describe-target-health \
  --target-group-arn arn:aws:elasticloadbalancing:us-west-1:867490540162:targetgroup/aa-cis-dev-api-tg/7632325fe7b91f96 \
  --region us-west-1 \
  --profile pqnghiep-admin \
  --query 'TargetHealthDescriptions[*].{Target:Target.Id,Health:TargetHealth.State}'
```

---

## 10. Quick Reference

| Action | Command |
|--------|---------|
| Deploy | `aws ecs update-service --force-new-deployment ...` |
| Rollback | `aws ecs update-service --task-definition aa-cis-dev-api:N ...` |
| Scale to 0 | `aws ecs update-service --desired-count 0 ...` |
| Health check | `curl https://api-cis.lumiguides.it.com/health` |
| View logs | `aws logs tail /ecs/aa-cis-dev --follow ...` |
| Cost check | `aws ce get-cost-and-usage ...` |
| Run tests | `pytest tests/integration/ -v` |
| Run E2E | `BASE_URL=http://localhost:3001 npx playwright test` |
| Run k6 | `k6 run -e BASE_URL=https://api-cis.lumiguides.it.com tests/load/k6_api_smoke_test.js` |

---

*AA-CIS Operations Runbook v1.0 · 20/04/2026 · Pham Quoc Nghiep*
