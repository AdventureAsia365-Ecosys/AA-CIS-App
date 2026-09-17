"""AA-606: Bedrock Batch Inference client for the S1-rewrite writer (attempt-1, Haiku).

Async, S3-mediated alternative to the synchronous per-tour invoke_model path (LLMClient.generate →
_call_bedrock_satellite) for rewriting many tours at once. Only the WRITER attempt-1 goes through
Batch — the judge (GPT-4.1, different vendor) and the generate↔judge retry loop stay on-demand.

Flow (all on the satellite account that owns the Batch permissions — AA-302/303 acc1, AA-399 acc3):
  1. build_manifest_jsonl()  — one JSONL record per tour: {recordId: tour_id, modelInput: <body>}
  2. upload input to  s3://<bronze>/batch-input/s1-rewrite/<job>/input.jsonl   (acc2 bronze bucket,
     cross-account read granted to the batch service role — modules/s3/main.tf)
  3. submit_batch_job()      — bedrock:CreateModelInvocationJob via get_satellite_client("bedrock", acct)
  4. poll_batch_job()        — bedrock:GetModelInvocationJob until a terminal status
  5. read_batch_output()     — parse s3://<bronze>/batch-output/s1-rewrite/<job>/.../<input>.out
                                → {tour_id: {text, usage, stop_reason} | {error}}

modelInput body is the EXACT Anthropic-on-Bedrock shape invoke_claude() sends (anthropic_version
bedrock-2023-05-31, system as cached content-blocks) so a batch record and a live call are
behaviourally identical. The caller (services/content_generation/s1_batch.py) then runs each
tour's decoded text through the same validate/judge/gate/persist path _execute_run_tour uses.

IAM boundary (verified in AA-CIS-Infra, both applied): acc1 role AA-Bedrock-Invoker and acc3 role
AA3-Bedrock-Invoker BOTH hold CreateModelInvocationJob + Get/ListModelInvocationJobs + PassRole on
their batch service role (bedrock_invoker_import.tf / accounts/acc3-bedrock/main.tf). The S3 grant
currently only covers the batch-input/batch-output ".../atom-decompose/*" prefixes — AA-606 adds
".../s1-rewrite/*" alongside (do NOT ship this module to prod before that Terraform applies, or the
batch service role gets AccessDenied reading the manifest).
"""
from __future__ import annotations

import io
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import structlog

from .bedrock_satellite import (
    ANTHROPIC_VERSION,
    _INFERENCE_PROFILES,
    _SATELLITE_ACCOUNTS,
    get_satellite_client,
)
from .prompt_cache import build_cached_system_prompt

logger = structlog.get_logger()

# Account 2 bronze bucket — Batch input/output live here; the satellite batch service role reads/
# writes it cross-account via bucket policy (s3BucketOwner mechanism, modules/s3/main.tf).
BRONZE_BUCKET = "aa-cis-bronze-005097885195"
INPUT_PREFIX = "batch-input/s1-rewrite"
OUTPUT_PREFIX = "batch-output/s1-rewrite"

# Batch service role assumed by Bedrock (PassRole'd by the invoker role) — one per account.
_BATCH_SERVICE_ROLE_ARN = {
    "acc1": "arn:aws:iam::867490540162:role/aa-bedrock-batch-inference-role",
    "acc3": "arn:aws:iam::786888028788:role/aa3-bedrock-batch-inference-role",
}

# Bedrock Batch minimum records per job for Claude models (AWS default is 100; a job with fewer is
# rejected at CreateModelInvocationJob). Kept as a module constant so the caller can pad/deny small
# batches; the real quota MUST be confirmed live before first prod run (AA-606 STEP0 open item).
MIN_BATCH_RECORDS = 100

_TERMINAL_STATES = {"Completed", "Failed", "Stopped", "PartiallyCompleted", "Expired"}


class BatchUnavailable(Exception):
    """Batch submit/poll/read failed. Caller falls back to the synchronous per-tour path (the
    existing LLMClient.generate flow), so a batch outage never blocks a rerun — just makes it slow."""
    pass


@dataclass
class BatchRecordResult:
    tour_id: str
    text: Optional[str] = None
    usage: dict = field(default_factory=dict)
    stop_reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)


def build_model_input(system: str, user: str, max_tokens: int = 4096) -> dict:
    """The per-record `modelInput` — identical to invoke_claude()'s InvokeModel body, incl. the
    AA-324 cached system content-blocks so batch records cache the same way live calls do."""
    body: dict = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        body["system"] = build_cached_system_prompt(system)
    return body


def build_manifest_jsonl(records: list[tuple[str, dict]]) -> str:
    """records = [(tour_id, model_input), ...] → JSONL string, one CreateModelInvocationJob record
    per line. recordId is the tour UUID so the output maps straight back to raw_tours.tour_id.

    Bedrock requires recordId to be unique per line and <= 256 chars — a tour_id UUID satisfies both.
    """
    lines = []
    seen = set()
    for tour_id, model_input in records:
        rid = str(tour_id)
        if rid in seen:
            raise BatchUnavailable(f"duplicate recordId in manifest: {rid}")
        seen.add(rid)
        lines.append(json.dumps({"recordId": rid, "modelInput": model_input}))
    return "\n".join(lines) + "\n"


def _s3_client(account: str):
    """S3 client for manifest upload / output read.

    The bronze bucket (aa-cis-bronze-005097885195) lives in acc2 — the SAME account the ECS task
    runs in. So WE read/write it with the task's own native identity (plain boto3, no AssumeRole),
    not the satellite session: the satellite invoker roles (AA-Bedrock-Invoker / AA3-Bedrock-Invoker)
    hold NO S3 permissions, only Bedrock ones. Cross-account access is only needed by the Batch
    SERVICE role that BEDROCK itself assumes at job time to read the input / write the output — that
    grant is on the bucket policy + the batch service role (modules/s3 + accounts/*-bedrock),
    separate from this client. `account` is accepted for signature symmetry but unused here.
    """
    import boto3
    return boto3.client("s3", region_name=_SATELLITE_ACCOUNTS.get(account, {}).get("region", "us-west-1"))


def upload_manifest(jsonl: str, *, account: str, job_slug: str) -> str:
    """Upload the JSONL manifest to the batch-input prefix; return its s3:// URI."""
    key = f"{INPUT_PREFIX}/{job_slug}/input.jsonl"
    try:
        s3 = _s3_client(account)
        s3.put_object(Bucket=BRONZE_BUCKET, Key=key,
                      Body=jsonl.encode("utf-8"), ContentType="application/jsonl")
    except Exception as e:
        raise BatchUnavailable(f"manifest upload failed ({key}): {type(e).__name__}: {e}") from e
    uri = f"s3://{BRONZE_BUCKET}/{key}"
    logger.info("s1_batch_manifest_uploaded", uri=uri, bytes=len(jsonl), account=account)
    return uri


def submit_batch_job(
    input_uri: str,
    *,
    account: str = "acc3",
    model: str = "haiku",
    job_name: Optional[str] = None,
    job_slug: Optional[str] = None,
) -> dict:
    """bedrock:CreateModelInvocationJob. Returns {"job_arn", "job_name", "output_uri", "account"}.

    account defaults to acc3 (the LLM primary, AA-397/399). model must be a key in
    _INFERENCE_PROFILES[account] ("haiku" for the S1 writer). Output goes to the batch-output prefix.
    """
    if account not in _SATELLITE_ACCOUNTS:
        raise BatchUnavailable(f"unknown account {account!r}")
    if account not in _BATCH_SERVICE_ROLE_ARN:
        raise BatchUnavailable(f"account {account!r} has no batch service role configured")
    model_id = _INFERENCE_PROFILES[account][model]
    slug = job_slug or uuid.uuid4().hex[:12]
    name = job_name or f"s1-rewrite-{slug}"
    output_uri = f"s3://{BRONZE_BUCKET}/{OUTPUT_PREFIX}/{slug}/"

    try:
        client = get_satellite_client("bedrock", account=account)
        resp = client.create_model_invocation_job(
            jobName=name,
            roleArn=_BATCH_SERVICE_ROLE_ARN[account],
            modelId=model_id,
            inputDataConfig={"s3InputDataConfig": {
                "s3Uri": input_uri,
                "s3BucketOwner": "005097885195",  # acc2 owns the bronze bucket (cross-account)
            }},
            outputDataConfig={"s3OutputDataConfig": {
                "s3Uri": output_uri,
                "s3BucketOwner": "005097885195",
            }},
        )
    except Exception as e:
        raise BatchUnavailable(
            f"CreateModelInvocationJob failed (account={account}, model={model}): {type(e).__name__}: {e}"
        ) from e

    job_arn = resp.get("jobArn")
    logger.info("s1_batch_job_submitted", job_arn=job_arn, job_name=name,
                account=account, model=model, input_uri=input_uri, output_uri=output_uri)
    return {"job_arn": job_arn, "job_name": name, "output_uri": output_uri,
            "account": account, "job_slug": slug}


def get_batch_status(job_arn: str, *, account: str = "acc3") -> dict:
    """bedrock:GetModelInvocationJob → {"status", "message"} (single, non-blocking check)."""
    try:
        client = get_satellite_client("bedrock", account=account)
        resp = client.get_model_invocation_job(jobIdentifier=job_arn)
    except Exception as e:
        raise BatchUnavailable(f"GetModelInvocationJob failed: {type(e).__name__}: {e}") from e
    return {"status": resp.get("status"), "message": resp.get("message")}


def poll_batch_job(
    job_arn: str,
    *,
    account: str = "acc3",
    interval_s: float = 30.0,
    timeout_s: float = 6 * 3600,
) -> str:
    """Block until the job reaches a terminal state or timeout. Returns the terminal status string.

    Intended for a background task, NOT an HTTP handler (a batch can take minutes to hours) — the
    endpoint layer (AA-606 admin_pipeline) submits then returns 202, and a poller drives this.
    """
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        st = get_batch_status(job_arn, account=account)
        status = st.get("status")
        if status != last:
            logger.info("s1_batch_poll", job_arn=job_arn, status=status, message=st.get("message"))
            last = status
        if status in _TERMINAL_STATES:
            return status
        time.sleep(interval_s)
    raise BatchUnavailable(f"batch job {job_arn} did not finish within {timeout_s}s (last={last})")


def _iter_output_keys(s3, output_uri: str):
    """List the .jsonl.out object(s) Bedrock wrote under the job's output prefix. Bedrock nests them
    under <output_uri>/<jobId>/<input-filename>.out — list the whole prefix and take .out files."""
    prefix = output_uri.replace(f"s3://{BRONZE_BUCKET}/", "").rstrip("/") + "/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BRONZE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".jsonl.out") or key.endswith(".out"):
                yield key


def read_batch_output(output_uri: str, *, account: str = "acc3") -> dict[str, BatchRecordResult]:
    """Parse the batch output .out file(s) → {tour_id: BatchRecordResult}.

    Each output line mirrors its input record: {"recordId": <tour_id>, "modelOutput": {...}} on
    success, or {"recordId": ..., "error": {...}} on a per-record failure. modelOutput is the same
    Anthropic InvokeModel response shape (content[0].text, usage, stop_reason)."""
    try:
        s3 = _s3_client(account)
        keys = list(_iter_output_keys(s3, output_uri))
    except Exception as e:
        raise BatchUnavailable(f"listing batch output failed ({output_uri}): {type(e).__name__}: {e}") from e

    if not keys:
        raise BatchUnavailable(f"no .out file found under {output_uri}")

    results: dict[str, BatchRecordResult] = {}
    for key in keys:
        try:
            body = s3.get_object(Bucket=BRONZE_BUCKET, Key=key)["Body"].read().decode("utf-8")
        except Exception as e:
            raise BatchUnavailable(f"reading batch output {key} failed: {type(e).__name__}: {e}") from e
        for line in io.StringIO(body):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("s1_batch_output_line_unparseable", key=key, line_len=len(line))
                continue
            rid = str(rec.get("recordId") or "")
            if not rid:
                continue
            if rec.get("error"):
                results[rid] = BatchRecordResult(tour_id=rid, error=json.dumps(rec["error"])[:1000])
                continue
            model_output = rec.get("modelOutput") or {}
            try:
                text = model_output["content"][0]["text"]
            except (KeyError, IndexError, TypeError):
                results[rid] = BatchRecordResult(
                    tour_id=rid, error=f"unexpected modelOutput shape: {str(model_output)[:300]}")
                continue
            results[rid] = BatchRecordResult(
                tour_id=rid,
                text=text,
                usage=model_output.get("usage", {}) or {},
                stop_reason=model_output.get("stop_reason"),
            )
    logger.info("s1_batch_output_parsed", output_uri=output_uri,
                records=len(results), ok=sum(1 for r in results.values() if r.ok))
    return results
