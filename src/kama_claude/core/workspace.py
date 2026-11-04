from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspacePathError(PermissionError):
    pass


@dataclass(frozen=True)
class PathAccess:
    workspace: Path
    requested_path: str
    target: Path
    outside_workspace: bool


class WorkspacePaths:
    # 捕获固定根目录，后续调用不依赖进程 cwd
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root if root is not None else Path.cwd()).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.root}")

    # 解析已有父目录和链接；新文件本身可以尚不存在
    def inspect(self, raw_path: str) -> PathAccess:
        if not raw_path.strip() or "\0" in raw_path:
            raise WorkspacePathError("invalid empty or null-containing path")
        try:
            requested = Path(raw_path)
            if requested.drive and not requested.is_absolute():
                raise ValueError("drive-relative paths are ambiguous; use an absolute path")
            target = (self.root / requested).resolve()
            # 非严格解析允许新文件；已有部分仍严格检查，不能吞掉无法解析的链接或权限错误
            ancestor = target
            while True:
                try:
                    ancestor.lstat()
                    break
                except FileNotFoundError:
                    if ancestor == ancestor.parent:
                        raise
                    ancestor = ancestor.parent
            ancestor.resolve(strict=True)
            if ancestor != target and not ancestor.is_dir():
                raise NotADirectoryError(f"parent path is not a directory: {ancestor}")
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError(f"cannot resolve path: {raw_path}") from exc
        return PathAccess(
            self.root, raw_path, target, not target.is_relative_to(self.root),
        )

    # 批准路径只能由执行入口显式传入，模型输入不能提供授权
    def resolve(self, raw_path: str, *, approved_path: Path | None = None) -> Path:
        access = self.inspect(raw_path)
        if approved_path is not None and access.target != approved_path:
            raise WorkspacePathError("target path changed; call the tool again for approval")
        if access.outside_workspace and approved_path is None:
            raise WorkspacePathError(f"outside workspace; approval required: {access.target}")
        return access.target
