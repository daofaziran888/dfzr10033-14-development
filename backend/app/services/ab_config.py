"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AbConfigService(BaseModuleService):
    MODULE = "ab_config"
    ENTITY = "AB实验"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["实验编号", "实验名称", "分流比例", "指标集合", "对照组", "实验组", "置信水平", "运行期间"]
    REQUIRED_FIELDS = ["实验编号", "实验名称", "分流比例"]
    STATUS_ORDER = ["草稿", "运行中", "已结束", "已终止"]
    ACTION_RULES = {"启动实验": "运行中", "结束实验": "已结束", "终止实验": "已终止"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记实验时显式带上分桶版本，避免曝光写入时版本不明。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["分桶版本"] = str(values.get("分桶版本") or "v1")
            entry["共享状态"] = "未发布"
        return entry, missing

    def experiment_summary(self, experiment_id: str) -> dict[str, Any]:
        """实验配置、分桶版本、分组共享状态和发布清单的联动快照。"""
        from app.services import experiment_common as ec

        rows = [row for row in store.rows(self.MODULE)
                if str(row.get("实验编号", "")) == experiment_id]
        if not rows:
            return {"ok": False, "message": f"实验 {experiment_id} 不存在"}
        experiment = rows[0]
        version = ec.latest_version(experiment_id)
        shares = ec.current_shares(experiment_id)
        release = next(
            (row for row in store.rows(ec.RELEASE_TABLE)
             if str(row.get("实验编号", "")) == experiment_id),
            None,
        )
        return {
            "ok": True,
            "实验编号": experiment_id,
            "实验名称": experiment.get("实验名称"),
            "status": experiment.get("status"),
            "分桶版本": version,
            "分桶配置": ec.bucket_configs(experiment_id, version),
            "分组共享": shares,
            "发布清单": release,
        }
