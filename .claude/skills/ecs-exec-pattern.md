# ECS Exec S3-Mediated Pattern

Use this pattern for ALL DB queries from a local machine. The canonical, maintained copy is
`AA-Ecosys/skill/aa-cis-schema/references/ecs-exec.md` in the private root repo; this file is a short
pointer for agents working only in this repo.

ECS container has no psql, no aws CLI — only python3 + asyncpg + boto3.
All infrastructure is on acc2 (profile `aa365-admin`, us-west-1). Never use another profile here: the
old acc1 stack is destroyed (AA-271) and wrong-account writes are what AA-517 cleaned up.
ECS Exec sessions are logged for 365 days: never print env vars or secret values.

## Steps (each command on one line)

1. Write the script to /tmp/script.py (it must upload its result to S3).
2. Find the bronze bucket (name not written here; this repo is public):
   `BUCKET=$(aws s3api list-buckets --query "Buckets[?starts_with(Name, 'aa-cis-bronze-')].Name | [0]" --output text --profile aa365-admin --region us-west-1)`
3. Upload: `aws s3 cp /tmp/script.py "s3://$BUCKET/scripts/script.py" --profile aa365-admin --region us-west-1`
4. Presign: `URL=$(aws s3 presign "s3://$BUCKET/scripts/script.py" --profile aa365-admin --region us-west-1 --expires-in 300)`
5. Get task: `TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)`
6. Run: `aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && python3 /tmp/s.py'" --profile aa365-admin --region us-west-1`
7. Fetch: `aws s3 cp "s3://$BUCKET/scripts/result.json" /tmp/result.json --profile aa365-admin --region us-west-1`

## Script template

```python
import asyncio, json, os, boto3
from urllib.parse import urlparse

async def main():
    import asyncpg
    sm = boto3.client("secretsmanager", region_name="us-west-1")
    dsn = sm.get_secret_value(SecretId="aa-cis/dev/rds")["SecretString"].strip()
    p = urlparse(dsn)
    conn = await asyncpg.connect(
        host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"), user=p.username, password=p.password,
        ssl="require"
    )
    # ... queries (schema-qualified) ...
    await conn.close()
    # Upload result to S3; BRONZE_BUCKET is set on the container by Terraform
    boto3.client("s3", region_name="us-west-1").put_object(
        Bucket=os.environ["BRONZE_BUCKET"], Key="scripts/result.json",
        Body=json.dumps(result, default=str), ContentType="application/json"
    )
    print(json.dumps(result, indent=2, default=str))

asyncio.run(main())
```
