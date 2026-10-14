"""Reject recognizable credential-shaped content; operators still sanitize business data."""

import json
import re

from app.factory_local.store import FactoryError

SENSITIVE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
    r"|\b(?:sk-[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})"
    r"|https?://[^\s/@:]+:[^\s/@]+@"
)


def ensure_sanitized(value):
    if SENSITIVE.search(json.dumps(value, ensure_ascii=False)):
        raise FactoryError("ARGUS_SENSITIVE_CONTENT")
