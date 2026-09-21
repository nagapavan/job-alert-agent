"""
Catalog of target organizations loaded from a user-supplied JSON config file.

The real, personal list lives in `backend/companies_config.json` (gitignored). When that
file is absent the shipped generic `backend/companies_config.example.json` is used so the
app has a working catalog out of the box without hardcoding any real employer names.
"""
import json
import math
from pathlib import Path
from typing import List, Dict, Optional

CONFIG_FILE = Path(__file__).parent / "companies_config.json"
EXAMPLE_CONFIG_FILE = Path(__file__).parent / "companies_config.example.json"

def load_company_catalog() -> List[Dict[str, str]]:
    """Loads target organizations from the user's JSON config, falling back to the shipped example."""
    for path in (CONFIG_FILE, EXAMPLE_CONFIG_FILE):
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []

TRENDING_COMPANY_CATALOG: List[Dict[str, str]] = load_company_catalog()

def get_catalog_company(name_or_domain: str) -> Optional[Dict[str, str]]:
    """
    Finds a catalog company by matching name (case-insensitive substring) or domain.
    """
    if not name_or_domain:
        return None
    query = name_or_domain.strip().lower()
    catalog = load_company_catalog()
    
    # Exact match on domain or name
    for c in catalog:
        if c.get("name", "").lower() == query or c.get("domain", "").lower() == query:
            return c
            
    # Substring match on name
    for c in catalog:
        c_name = c.get("name", "").lower()
        if query in c_name or c_name in query:
            return c
            
    return None

def compute_cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculates cosine similarity between two vector lists."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot_product / (norm_a * norm_b)))

def parse_company_import(content: str, filename: str = "") -> List[Dict[str, str]]:
    """
    Parses company entries from CSV, JSON, Markdown, or plain text content.
    Returns a list of standardized company dicts.
    """
    import csv
    import io
    import re

    if not content or not content.strip():
        return []

    text = content.strip()
    lower_fn = filename.lower()
    results: List[Dict[str, str]] = []

    # 1. Try JSON
    if lower_fn.endswith(".json") or text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k in ["companies", "data", "results", "items", "target_companies"]:
                    if k in data and isinstance(data[k], list):
                        data = data[k]
                        break
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("company") or item.get("company_name") or item.get("organization") or ""
                        if name and name.strip():
                            results.append({
                                "name": str(name).strip(),
                                "domain": str(item.get("domain") or item.get("website") or item.get("url") or "").strip(),
                                "careers_url": str(item.get("careers_url") or item.get("careers") or item.get("jobs_url") or "").strip(),
                                "description": str(item.get("description") or item.get("notes") or "").strip()
                            })
                    elif isinstance(item, str) and item.strip():
                        results.append({"name": item.strip(), "domain": "", "careers_url": "", "description": ""})
                if results:
                    return results
        except Exception:
            pass

    # 2. Try Markdown Table / List if .md or contains markdown markers
    if lower_fn.endswith(".md") or ("|" in text and "\n" in text) or text.startswith("- ") or text.startswith("* "):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        # Check if Markdown table
        table_lines = [l for l in lines if l.startswith("|") and l.endswith("|")]
        if len(table_lines) >= 2:
            headers = [h.strip().lower() for h in table_lines[0].strip("|").split("|")]
            name_idx = -1
            domain_idx = -1
            careers_idx = -1
            desc_idx = -1

            for idx, h in enumerate(headers):
                if any(k in h for k in ["company", "name", "organization"]):
                    name_idx = idx
                elif any(k in h for k in ["domain", "website"]):
                    domain_idx = idx
                elif any(k in h for k in ["careers", "portal", "job", "url"]):
                    careers_idx = idx
                elif any(k in h for k in ["desc", "notes", "summary"]):
                    desc_idx = idx

            # Fallback column positions
            if name_idx == -1: name_idx = 0
            if domain_idx == -1 and len(headers) > 1: domain_idx = 1
            if careers_idx == -1 and len(headers) > 2: careers_idx = 2

            for row_line in table_lines[1:]:
                # Skip markdown separator row |---|---|
                if re.match(r"^\|[\s\-:|]+\|$", row_line):
                    continue
                cells = [c.strip() for c in row_line.strip("|").split("|")]
                if len(cells) > name_idx and cells[name_idx]:
                    name_val = cells[name_idx]
                    dom_val = cells[domain_idx] if domain_idx != -1 and len(cells) > domain_idx else ""
                    car_val = cells[careers_idx] if careers_idx != -1 and len(cells) > careers_idx else ""
                    desc_val = cells[desc_idx] if desc_idx != -1 and len(cells) > desc_idx else ""
                    results.append({
                        "name": name_val,
                        "domain": dom_val,
                        "careers_url": car_val,
                        "description": desc_val
                    })
            if results:
                return results

        # Check Markdown bullet lists: - Company (domain.com) - https://portal.com
        for line in lines:
            if line.startswith(("- ", "* ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                clean_line = re.sub(r"^[\-\*\d\.\s]+", "", line).strip()
                if not clean_line:
                    continue
                
                # Match URL if present
                url_match = re.search(r"https?://[^\s\)]+", clean_line)
                url_val = url_match.group(0) if url_match else ""
                clean_no_url = clean_line.replace(url_val, "").strip(" -–:,()")

                # Match domain in parentheses e.g. (stripe.com)
                dom_match = re.search(r"\(([a-zA-Z0-9\.\-]+\.[a-zA-Z]{2,})\)", clean_no_url)
                dom_val = dom_match.group(1) if dom_match else ""
                clean_name = re.sub(r"\([^\)]+\)", "", clean_no_url).split(" - ")[0].split(":")[0].strip()

                if clean_name:
                    results.append({
                        "name": clean_name,
                        "domain": dom_val,
                        "careers_url": url_val,
                        "description": ""
                    })
        if results:
            return results

    # 3. Try CSV / Delimited lines
    try:
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if r and any(c.strip() for c in r)]
        if rows:
            header_row = [c.strip().lower() for c in rows[0]]
            has_named_header = any(k in "".join(header_row) for k in ["name", "company", "domain", "careers", "url"])

            name_idx = 0
            domain_idx = 1 if len(header_row) > 1 else -1
            careers_idx = 2 if len(header_row) > 2 else -1
            desc_idx = 3 if len(header_row) > 3 else -1

            start_idx = 0
            if has_named_header:
                start_idx = 1
                for idx, h in enumerate(header_row):
                    if any(k in h for k in ["company", "name", "organization"]):
                        name_idx = idx
                    elif any(k in h for k in ["domain", "website"]):
                        domain_idx = idx
                    elif any(k in h for k in ["careers", "portal", "job", "url"]):
                        careers_idx = idx
                    elif any(k in h for k in ["desc", "notes", "summary"]):
                        desc_idx = idx

            for row in rows[start_idx:]:
                if len(row) > name_idx and row[name_idx].strip():
                    name_val = row[name_idx].strip()
                    dom_val = row[domain_idx].strip() if domain_idx != -1 and len(row) > domain_idx else ""
                    car_val = row[careers_idx].strip() if careers_idx != -1 and len(row) > careers_idx else ""
                    desc_val = row[desc_idx].strip() if desc_idx != -1 and len(row) > desc_idx else ""
                    results.append({
                        "name": name_val,
                        "domain": dom_val,
                        "careers_url": car_val,
                        "description": desc_val
                    })
    except Exception:
        pass

    # 4. Fallback line-by-line names
    if not results:
        for line in text.splitlines():
            line_str = line.strip(" -*,")
            if line_str and not line_str.startswith("#"):
                results.append({"name": line_str, "domain": "", "careers_url": "", "description": ""})

    return results

