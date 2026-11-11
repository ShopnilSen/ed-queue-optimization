#!/usr/bin/env python3
"""Fail-closed checks for the public, software-only release tree.

This guard is deliberately conservative. It rejects research artifacts,
credential-like strings, and private material, and it never prints a matched
value. Run it from the repository root before committing or pushing:

    python scripts/verify_public_release.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 2_000_000

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
    ".eggs",
}

# Top-level directories that belong to the private study, never this release.
FORBIDDEN_TOP_DIRS = {
    ".autoresearch",
    "archive",
    "audit",
    "backups",
    "data",
    "datasets",
    "documents",
    "experiments",
    "figures",
    "literature",
    "manuscript",
    "manuscripts",
    "models",
    "notes",
    "paper",
    "papers",
    "private",
    "raw_data",
    "reports",
    "research",
    "results",
    "submission",
    "verification",
}

FORBIDDEN_SUFFIXES = {
    ".7z",
    ".aux",
    ".bbl",
    ".bib",
    ".bz2",
    ".ckpt",
    ".csv",
    ".db",
    ".dta",
    ".doc",
    ".docx",
    ".feather",
    ".gguf",
    ".gz",
    ".h5",
    ".ipynb",
    ".joblib",
    ".jpeg",
    ".jpg",
    ".log",
    ".npy",
    ".npz",
    ".onnx",
    ".parquet",
    ".pdf",
    ".pkl",
    ".pickle",
    ".png",
    ".ppt",
    ".pptx",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".sas7bdat",
    ".sav",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".tex",
    ".tif",
    ".tiff",
    ".toc",
    ".tsv",
    ".xls",
    ".xlsx",
    ".xz",
    ".zip",
}

TEXT_NAMES = {".gitignore", ".env.example", "notice"}
TEXT_SUFFIXES = {".cfg", ".example", ".ini", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}

CREDENTIAL_DATA_SUFFIXES = {".json", ".toml", ".txt", ".yaml", ".yml"}

SECRET_PATTERNS = [
    ("openai-style key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic-style key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    (
        "github token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("hugging face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    (
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "generic credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret|token|password|passwd|authorization)\b"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{20,}"
        ),
    ),
    (
        "credential in url",
        re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]{3,}:[^/\s@]{6,}@", re.IGNORECASE),
    ),
]

# Private material that must never appear in the public tree.
FORBIDDEN_TEXT_PATTERNS = [
    ("private workspace path", re.compile(r"[Cc]:[\\/]triage")),
    ("private e-mail address", re.compile(r"lazzystifner@gmail\.com", re.IGNORECASE)),
    ("private e-mail address", re.compile(r"shopnilsen@scu\.edu\.cn", re.IGNORECASE)),
    ("private study dataset", re.compile(r"\bNHAMCS\b", re.IGNORECASE)),
    ("private study output", re.compile(r"\b(?:all|test)_predictions\b", re.IGNORECASE)),
    ("private study output", re.compile(r"\bcohort\.csv\b", re.IGNORECASE)),
]

REQUIRED_FILES = {
    "LICENSE",
    "NOTICE",
    "README.md",
    ".env.example",
    "pyproject.toml",
    "scripts/verify_public_release.py",
}


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in TEXT_NAMES


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def check_tree(root: Path) -> tuple[list[str], dict[str, int]]:
    root = Path(root).resolve()
    errors: list[str] = []
    stats = {"files": 0, "text_files": 0, "bytes": 0}

    if not root.is_dir():
        return [f"root is not a directory: {root}"], stats

    for required in sorted(REQUIRED_FILES):
        if not (root / required).is_file():
            errors.append(f"{required}: required release file is missing")

    for path in _iter_files(root):
        rel = path.relative_to(root)
        rel_posix = rel.as_posix()
        top_part = rel.parts[0].lower() if rel.parts else ""
        lower_name = path.name.lower()
        suffix = path.suffix.lower()

        stats["files"] += 1
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"{rel_posix}: cannot stat file ({type(exc).__name__})")
            continue
        stats["bytes"] += size

        if path.is_symlink():
            errors.append(f"{rel_posix}: symlinks are not allowed in the release")
            continue

        if top_part in FORBIDDEN_TOP_DIRS:
            errors.append(f"{rel_posix}: forbidden top-level directory ({top_part})")

        if suffix in FORBIDDEN_SUFFIXES:
            errors.append(f"{rel_posix}: forbidden file type ({suffix})")

        if lower_name in {"nul", "con", "prn", "aux", "com1", "lpt1"}:
            errors.append(f"{rel_posix}: reserved filesystem name")

        if lower_name == ".env" or lower_name == ".envrc" or (
            lower_name.startswith(".env.") and lower_name != ".env.example"
        ):
            errors.append(f"{rel_posix}: environment file with possible secrets")

        if (
            suffix in CREDENTIAL_DATA_SUFFIXES
            and ("credential" in lower_name or "secret" in lower_name)
            and lower_name not in {".env.example"}
        ):
            errors.append(f"{rel_posix}: credential data file is not permitted")

        if size > MAX_FILE_BYTES:
            errors.append(f"{rel_posix}: unexpected large file ({size} bytes)")
            continue

        if not _is_text(path):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{rel_posix}: cannot read text file ({type(exc).__name__})")
            continue

        stats["text_files"] += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    errors.append(
                        f"{rel_posix}: possible credential pattern ({label}) at line {lineno}"
                    )
            for label, pattern in FORBIDDEN_TEXT_PATTERNS:
                if pattern.search(line):
                    errors.append(f"{rel_posix}: {label} at line {lineno}")

    return errors, stats


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Check the public release tree for forbidden content.")
    parser.add_argument("--root", type=Path, default=default_root, help="release root to scan")
    args = parser.parse_args(argv)

    errors, stats = check_tree(args.root)
    print(
        f"Scanned {stats['files']} files "
        f"({stats['text_files']} text files, {stats['bytes']} bytes)."
    )

    if errors:
        print("FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("PASS: no research artifacts, private paths, or credential patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
