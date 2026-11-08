from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".turbo",
}
SKIP_FILES = {".env.example", "package-lock.json"}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".woff",
    ".woff2",
    ".ttf",
    ".pyc",
    ".so",
    ".dylib",
    ".lock",
}
MAX_BYTES = 2 * 1024 * 1024
ALLOW_MARKER = "scan:allow"
PLACEHOLDER = re.compile(
    r"(your[-_]|change[-_]?me|example|placeholder|xxxx|<[^>]+>|\$\{|dummy|sample|fake|"
    r"redacted|not[-_]a[-_]real|test[-_]?(secret|key|token|admin)|0000|1234)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # which group holds the secret value


RULES = [
    Rule("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    Rule("PayPal access token", re.compile(r"\bA21AA[A-Za-z0-9_\-]{40,}")),
    Rule(
        "PayPal client secret",
        re.compile(r"""(?i)paypal[\w\-]*secret["']?\s*[:=]\s*["']?([A-Za-z0-9_\-]{20,})"""),
        1,
    ),
    Rule("PayPal secret-shaped value", re.compile(r"\bE[A-Za-z0-9_\-]{70,90}\b")),
    Rule(
        "bearer token",
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=\-]{24,})"),
        1,
    ),
    Rule("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    Rule("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    Rule("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Rule("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    Rule("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule(
        "secret-looking assignment",
        re.compile(
            r"""(?i)\b[\w\-]*(?:secret|api[_\-]?key|auth[_\-]?token|access[_\-]?token|password)"""
            r"""["']?\s*[:=]\s*["']([A-Za-z0-9/+_\-]{24,})["']"""
        ),
        1,
    ),
    Rule(
        "secret-looking env value",
        re.compile(
            r"""(?im)^\s*[A-Z0-9_]*(?:SECRET|API_KEY|TOKEN|PASSWORD)\s*=\s*([A-Za-z0-9/+_\-]{24,})\s*$"""
        ),
        1,
    ),
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    preview: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.preview}"


def mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-2:]} ({len(value)} chars)"


def iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    for base in paths:
        if base.is_file():
            yield base
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                yield Path(current) / name


def scan_text(text: str, shown_path: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                value = match.group(rule.group)
                if rule.group and PLACEHOLDER.search(value):
                    continue
                findings.append(Finding(shown_path, number, rule.name, mask(value)))
    return findings


def scan_file(path: Path, root: Path = ROOT) -> list[Finding]:
    if path.name in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES:
        return []
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw[:4096]:
        return []  # binary
    try:
        shown = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        shown = str(path)
    return scan_text(raw.decode("utf-8", errors="replace"), shown)


def git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def check_env_files_ignored() -> list[str]:
    """Problems with .env handling. Empty outside a git checkout (nothing to check)."""
    inside = git("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0:
        return []
    problems: list[str] = []
    for name in (".env", ".env.local", "frontend/.env.local"):
        result = git("check-ignore", "-q", name)
        if result is not None and result.returncode == 1:
            problems.append(f"{name} is NOT git-ignored")
    tracked = git("ls-files")
    if tracked is not None:
        for entry in tracked.stdout.splitlines():
            base = Path(entry).name
            if base.startswith(".env") and base != ".env.example":
                problems.append(f"{entry} is tracked by git; remove it with `git rm --cached`")
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(arg) for arg in argv] or [ROOT]
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        print(f"scan_secrets: not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    findings: list[Finding] = []
    scanned = 0
    for path in iter_files(targets):
        scanned += 1
        findings.extend(scan_file(path))
    problems = check_env_files_ignored() if not argv else []
    for finding in findings:
        print(finding)
    for problem in problems:
        print(f"env: {problem}")
    if findings or problems:
        print(f"\nscan_secrets: FOUND {len(findings)} secret(s), {len(problems)} env problem(s).")
        return 1
    print(f"scan_secrets: clean ({scanned} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))