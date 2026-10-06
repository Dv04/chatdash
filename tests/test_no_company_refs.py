"""The public product must not carry the maintainer's company-specific workflow text, paths or domains (work item ids like DHI-12 are fine).
Only the credit line ("built by ... at DHI", linking the maintainer's site) is allowed to name them."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN = ["dhi_orbit", "tools", "docs", "contrib", "README.md"]
EXT = {".py", ".js", ".html", ".css", ".json", ".md", ".toml", ".sh", ".txt", ".yml", ".yaml", ".rb"}
FORBIDDEN = re.compile(r"dhi-fleet|/Users/apple|aixavier|trydhi|vmukti|workstreams?/|WORKSTREAMS|dhi-tech\.com|dhi-tech", re.I)
CREDIT = re.compile(r"built by .{0,40}at\b", re.I)


def test_shipped_files_name_no_company_workflows_paths_or_domains():
    bad = []
    for entry in SCAN:
        base = ROOT / entry
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for p in files:
            if p.suffix not in EXT or any(x in p.parts for x in ("__pycache__", "node_modules", ".egg-info")):
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for n, line in enumerate(lines, 1):
                near = line + (lines[n - 2] if n > 1 else "")   # the credit link may wrap onto the line after "built by"
                if FORBIDDEN.search(line) and not CREDIT.search(near):
                    bad.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not bad, "company-specific references in the public tree:\n" + "\n".join(bad)
