from enum import StrEnum


class PermissionMode(StrEnum):
    ASK = "ask"          # 沿用逐工具审批与精确文件授权。
    APPROVE = "approve"  # 放宽普通操作，高危命令和外部写入仍询问。
    FULL = "full"        # 跳过询问；明确禁止和工具自身约束始终有效。
