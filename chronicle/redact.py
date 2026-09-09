"""Secret scrubbing. Runs at ingest so credentials never reach the index."""
import re

_RULES = [
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_KEY",    re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}")),
    ("GITHUB_PAT",    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GITHUB_PAT",    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("SHOPIFY_TOKEN", re.compile(r"\bshp(?:at|ca|pa|ss)_[A-Fa-f0-9]{20,}")),
    ("AWS_KEY",       re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GOOGLE_KEY",    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("SLACK_TOKEN",   re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("STRIPE_KEY",    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("JWT",           re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("PRIVATE_KEY",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("BEARER",        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("PASSWORD",      re.compile(r"(?i)([\"']?(?:password|passwd|secret|api[_-]?key|access[_-]?token)[\"']?\s*[:=]\s*[\"'])([^\"'\n]{6,})([\"'])")),
]

def scrub(text):
    """Return (clean_text, n_redactions)."""
    if not text:
        return text, 0
    n = 0
    for label, rx in _RULES:
        if label == "PASSWORD":
            text, k = rx.subn(lambda m: f"{m.group(1)}[REDACTED:PASSWORD]{m.group(3)}", text)
        else:
            text, k = rx.subn(f"[REDACTED:{label}]", text)
        n += k
    return text, n
