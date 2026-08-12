"""Validate that the exportable repository is safe for public distribution."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = {
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Feishu webhook": re.compile(
        r"open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F-]{20,}"),
}
BRANDING_PATTERN = re.compile(
    "|".join(("cl" + "aude", "anth" + "ropic", r"\.cl" + "aude")),
    re.IGNORECASE)
PRIVATE_PATH_PATTERN = re.compile(
    re.escape("/" + "Users" + "/") + r"[^/\s]+/|"
    + r"[A-Za-z]:\\" + "Users" + r"\\",
    re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
ENV_ASSIGNMENT = re.compile(r"(?m)^([A-Z][A-Z0-9_]*)=")


def _git(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, input=input_text, text=True,
        capture_output=True, check=True)
    return result.stdout


def _tracked_files() -> list[Path]:
    return [ROOT / p for p in _git("ls-files").splitlines() if p]


def _export_ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    output = _git("check-attr", "export-ignore", "--", rel).strip()
    return output.endswith(": set")


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _check_markdown(path: Path, text: str, exportable: set[Path]) -> list[str]:
    errors: list[str] = []
    for match in MARKDOWN_LINK.finditer(text):
        raw = match.group(1).strip().strip("<>")
        if not raw or raw.startswith(("#", "http://", "https://", "mailto:")):
            continue
        target_text = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if not target_text:
            continue
        target = (path.parent / target_text).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            errors.append(f"{path.relative_to(ROOT)}: link escapes repository: {raw}")
            continue
        if not target.exists():
            errors.append(f"{path.relative_to(ROOT)}: broken link: {raw}")
        elif target.is_file() and target not in exportable:
            errors.append(f"{path.relative_to(ROOT)}: link targets private export: {raw}")
    return errors


def main() -> int:
    tracked = _tracked_files()
    exportable = {p.resolve() for p in tracked if not _export_ignored(p)}
    errors: list[str] = []

    forbidden_roots = ("data/", "backups/", "secrets/", "artifacts/", "docs/evidence/")
    for path in sorted(exportable):
        rel = path.relative_to(ROOT).as_posix()
        if rel == ".env" or rel.startswith(forbidden_roots):
            errors.append(f"forbidden public path: {rel}")
            continue
        text = _text(path)
        if text is None:
            continue
        if BRANDING_PATTERN.search(text):
            errors.append(f"disallowed provider branding: {rel}")
        if PRIVATE_PATH_PATTERN.search(text):
            errors.append(f"private workstation path: {rel}")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{label} pattern: {rel}")
        if path.suffix.lower() == ".md":
            errors.extend(_check_markdown(path, text, exportable))

    required = {
        "README.md", "README.zh-CN.md", "LICENSE", "SECURITY.md",
        "CONTRIBUTING.md", ".env.example",
    }
    present = {p.relative_to(ROOT).as_posix() for p in exportable}
    for rel in sorted(required - present):
        errors.append(f"missing public file: {rel}")

    readme = ROOT / "README.md"
    if readme in exportable:
        readme_text = _text(readme) or ""
        if not readme_text.startswith("# Personal Trading Decision System (IBKR)"):
            errors.append("README.md is not the English default")
        if "README.zh-CN.md" not in readme_text:
            errors.append("README.md does not link to the Chinese version")

    env_en = ROOT / ".env.example"
    env_zh = ROOT / ".env.example.zh-CN"
    if env_en in exportable and env_zh in exportable:
        vars_en = set(ENV_ASSIGNMENT.findall(_text(env_en) or ""))
        vars_zh = set(ENV_ASSIGNMENT.findall(_text(env_zh) or ""))
        if vars_en != vars_zh:
            errors.append("English and Chinese environment templates differ: "
                          f"en_only={sorted(vars_en - vars_zh)} "
                          f"zh_only={sorted(vars_zh - vars_en)}")

    if errors:
        print("PUBLIC_HYGIENE_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"PUBLIC_HYGIENE_OK files={len(exportable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
