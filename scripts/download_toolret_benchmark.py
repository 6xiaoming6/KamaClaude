"""Download official ToolRet evaluation snapshots at immutable HF revisions."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1] / "runs/mcp-tool-routing-20261116/toolret-data"


def fetch_file(repo: str, revision: str, path: str) -> dict:
    target = ROOT / repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            response = client.get(f"https://huggingface.co/datasets/mangopy/{repo}/resolve/{revision}/{path}")
            response.raise_for_status()
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(response.content)
        temporary.replace(target)
    return {"path": str(target.relative_to(ROOT)), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size}


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "manifest.json"
    if manifest_path.exists():
        repos = json.loads(manifest_path.read_bytes())["repositories"]
    else:
        repos = {}
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for repo in ("ToolRet-Tools", "ToolRet-Queries"):
                response = client.get(f"https://huggingface.co/api/datasets/mangopy/{repo}")
                response.raise_for_status()
                info = response.json()
                paths = [item["rfilename"] for item in info["siblings"]
                         if item["rfilename"].endswith(".parquet") or item["rfilename"] == "README.md"]
                repos[repo] = {"revision": info["sha"], "paths": sorted(paths)}
    # Save the frozen versions before downloading, so restarts cannot change revisions.
    manifest_path.write_text(json.dumps({"repositories": repos}, indent=2), encoding="utf-8")
    tasks = [(repo, meta["revision"], path) for repo, meta in repos.items() for path in meta["paths"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_file, *task) for task in tasks]
        files = []
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            files.append(row)
            print("Downloaded", row["path"], row["bytes"], flush=True)
    manifest_path.write_text(json.dumps({"repositories": repos, "files": sorted(files, key=lambda x: x["path"])},
                                        indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
