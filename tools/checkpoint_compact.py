"""Generic offline checkpoint compactor. Standard library only.
Usage: python tools/checkpoint_compact.py state.json index.json
       python tools/checkpoint_compact.py --self-test
Prints summary to stdout; never overwrites input, never calls a network.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

def norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

def canonical_url(value):
    try:
        p = urlsplit(str(value or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return ""
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))
    except ValueError:
        return ""

def summarize(state, records):
    if not isinstance(state, dict) or not isinstance(records, list):
        raise ValueError("Expected a state object and a list of record objects")
    seen, duplicates, unidentified = {}, [], []
    for i, item in enumerate(records):
        if not isinstance(item, dict):
            raise ValueError("Each record must be an object")
        title = norm(item.get("title"))
        year = norm(item.get("year"))
        doi = norm(item.get("doi")).removeprefix("https://doi.org/")
        url = canonical_url(item.get("url"))
        keys = ([("doi", doi)] if doi else []) + ([("url", url)] if url else []) + ([("title_year", title+"|"+year)] if title and year else [])
        if not keys:
            unidentified.append(i)
            continue
        matches = sorted({seen[k] for k in keys if k in seen})
        if matches:
            duplicates.append({"row": i, "matches": matches})
        for k in keys:
            seen.setdefault(k, i)
    allowed = ("project_id", "version", "batch", "counts", "next_task", "artifact_refs", "blockers")
    compact = {k: state[k] for k in allowed if k in state}
    compact["index_rows"] = len(records)
    compact["duplicate_candidates"] = len(duplicates)
    compact["unidentified_rows"] = len(unidentified)
    compact["index_sha256"] = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"checkpoint": compact, "review": {"duplicates": duplicates, "unidentified": unidentified}}

def self_test():
    state = {"batch": 2, "counts": {"accepted": 1}, "unnecessary": "not exported"}
    rows = [{"title": "Alpha", "year": 2024, "doi": "10.1/example"}, {"title": "ALPHA", "year": 2024}, {}]
    result = summarize(state, rows)
    assert result["checkpoint"]["duplicate_candidates"] == 1
    assert result["checkpoint"]["unidentified_rows"] == 1
    assert result["checkpoint"]["counts"] == {"accepted": 1}
    assert "unnecessary" not in result["checkpoint"]
    print("PASS")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("state", nargs="?")
    p.add_argument("index", nargs="?")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return
    if not a.state or not a.index:
        p.error("state and index are required")
    read = lambda x: json.loads(Path(x).read_text(encoding="utf-8"))
    print(json.dumps(summarize(read(a.state), read(a.index)), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
