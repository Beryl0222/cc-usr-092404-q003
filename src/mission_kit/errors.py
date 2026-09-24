"""mission_kit 统一异常类型。"""

from __future__ import annotations


class MissionKitError(Exception):
    """所有领域错误的基类。"""


class ContractError(MissionKitError):
    """数据不满足当前合同：字段缺失、类型不符或语义非法。"""


class UnsupportedVersionError(ContractError):
    """加载器遇到无法接纳的 schema 版本（超前或已淘汰）。

    超前版本绝不能按本地规则结算，只能封存原文等待加载器升级。
    """

    def __init__(self, version: int, *, current: int, supported_legacy: tuple[int, ...]):
        self.version = version
        self.current = current
        self.supported_legacy = supported_legacy
        if version > current:
            msg = (
                f"schema_version={version} 超前于加载器当前版本 {current}："
                "必须封存原文与摘要并标记 AWAITING_UPGRADE，不得按本地规则解释其校准/单位语义"
            )
        else:
            msg = (
                f"schema_version={version} 已淘汰且不受支持（当前版本 {current}，"
                f"可迁移旧版本 {sorted(supported_legacy)}）"
            )
        super().__init__(msg)


class MigrationRequiredError(ContractError):
    """受支持的旧版本记录未获得显式迁移许可，fail-closed 拒绝结算。"""

    def __init__(self, record_id: str, version: int, *, current: int):
        self.record_id = record_id
        self.version = version
        self.current = current
        super().__init__(
            f"记录 {record_id} 使用受支持的旧版本 schema_version={version}，"
            f"必须显式迁移到版本 {current} 并留下迁移记录后才能结算"
        )


class MalformedDocumentError(ContractError):
    """文档无法解析为 JSON 对象。"""
