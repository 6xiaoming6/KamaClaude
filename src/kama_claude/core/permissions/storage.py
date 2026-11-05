from __future__ import annotations

from pathlib import Path

_DEFAULT_POLICY_PATH = Path("~/.kama/policy.toml")


# 加载 policy.toml 中 [always] 节，返回 {tool_name: "allow"/"deny"}；文件不存在时返回空字典
def load_policy_file(path: Path | None = None) -> dict[str, str]:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    if not p.exists():
        return {}
    result: dict[str, str] = {}
    in_always = False
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[always]":
            in_always = True
            continue
        if stripped.startswith("["):
            in_always = False
            continue
        if in_always and "=" in stripped and not stripped.startswith("#"):
            k, _, v = stripped.partition("=")
            k = k.strip()
            v = v.strip().strip('"')
            if v in ("allow", "deny"):
                result[k] = v
    return result
