"""shared/aws_client/ses_mailer.py — AA-828 operator e-mail through Amazon SES v2.

SES identity `lumiguides.it.com` on acc2 (AA-CIS-Infra ses.tf, AA-811). The CIS task role may send
only as the Terraform `ses_from_address` (IAM condition `ses:FromAddress`) and only through the
`transactional` configuration set, so both come from the environment the task definition sets:

  ALERT_EMAIL_TO          operator mailbox (Dev: nghieppham@adventure.asia)
  SES_FROM_ADDRESS        must equal var.ses_from_address (no-reply@lumiguides.it.com)
  SES_CONFIGURATION_SET   aa-email-dev-transactional
  SES_REGION              optional, default AWS_REGION / us-west-1

`ALERT_EMAIL_TO` or `SES_FROM_ADDRESS` empty -> `alert_mail_config()` returns None and callers log and
send nothing. AA-816: `sender_config()` is the same sender without the operator mailbox, for the staff
invitation (services/jobs/staff_invite_email_job.py). Used from job handlers only, never from a request
or an LLM call path: the boto3 call blocks, so it runs in a thread.

boto3 is imported lazily; tests inject a stub client (anything with
`send_email(**kwargs) -> {"MessageId": ...}`). Same contract as AA-Booking app/mailer.py.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol


class SesClient(Protocol):
    def send_email(self, **kwargs: Any) -> dict[str, Any]: ...


# SES answers that resending the same message can never fix (bad address, sender not allowed, account
# paused). Everything else (throttling, 5xx, network) is retried by the job runner. AccessDenied is NOT
# here: right after the Infra apply that grants ses:SendEmail, IAM propagation can deny the first sends.
PERMANENT_ERROR_CODES = frozenset({
    "MessageRejected",
    "MailFromDomainNotVerifiedException",
    "AccountSuspendedException",
    "SendingPausedException",
    "BadRequestException",
    "NotFoundException",
})


class MailError(Exception):
    """SES refused the message. `permanent` says whether a retry can help."""

    def __init__(self, code: str, message: str, permanent: bool = False) -> None:
        super().__init__(code, message, permanent)
        self.code = code
        self.message = message
        self.permanent = permanent

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class AlertMailConfig:
    to: str
    sender: str
    configuration_set: Optional[str]
    region: str


def alert_mail_config() -> Optional[AlertMailConfig]:
    """The operator alert e-mail settings, or None when alert e-mail is not configured."""
    to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    sender = os.environ.get("SES_FROM_ADDRESS", "").strip()
    if not to or not sender:
        return None
    return AlertMailConfig(
        to=to, sender=sender,
        configuration_set=os.environ.get("SES_CONFIGURATION_SET", "").strip() or None,
        region=(os.environ.get("SES_REGION") or os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"),
    )


@dataclass(frozen=True)
class SenderConfig:
    """AA-816: who CIS mail is sent as, for mail whose recipient is not the operator (staff invitations)."""
    sender: str
    configuration_set: Optional[str]
    region: str


def sender_config() -> Optional[SenderConfig]:
    """The SES sender settings (SES_FROM_ADDRESS, SES_CONFIGURATION_SET, region), or None when
    SES_FROM_ADDRESS is unset. Unlike alert_mail_config() it does not need ALERT_EMAIL_TO."""
    sender = os.environ.get("SES_FROM_ADDRESS", "").strip()
    if not sender:
        return None
    return SenderConfig(
        sender=sender,
        configuration_set=os.environ.get("SES_CONFIGURATION_SET", "").strip() or None,
        region=(os.environ.get("SES_REGION") or os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"),
    )


def ses_client(region: str) -> SesClient:
    import boto3  # lazy: only the worker path that sends mail needs it

    return boto3.client("sesv2", region_name=region)


def _error_code(exc: BaseException) -> Optional[str]:
    """The SES error code of a botocore ClientError (read duck-typed, so botocore is not imported)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str):
            return code
    return None


async def send(client: SesClient, message: Message, *, sender: str,
               configuration_set: Optional[str] = None) -> str:
    """Send one message; returns the SES MessageId. Raises MailError on an SES refusal."""
    kwargs: dict[str, Any] = {
        "FromEmailAddress": sender,
        "Destination": {"ToAddresses": [message.to]},
        "Content": {
            "Simple": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": message.text, "Charset": "UTF-8"},
                    "Html": {"Data": message.html, "Charset": "UTF-8"},
                },
            }
        },
    }
    if configuration_set:
        kwargs["ConfigurationSetName"] = configuration_set
    try:
        response = await asyncio.to_thread(client.send_email, **kwargs)
    except Exception as exc:
        code = _error_code(exc)
        if code is None:
            raise
        raise MailError(code, str(exc)[:500], permanent=code in PERMANENT_ERROR_CODES) from exc
    return str(response["MessageId"])
