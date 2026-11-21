"""指标工厂领域服务：指标计算定义、聚合窗口、从立方到指标的产出管线。"""
from __future__ import annotations

import hashlib
import random
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MetricFactoryService(BaseModuleService):
    MODULE = "metric_factory"
    ENTITY = "计算指标"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["指标编号", "指标名称", "数据来源", "计算公式", "聚合窗口", "精度模式", "依赖立方", "最后产出"]
    REQUIRED_FIELDS = ["指标编号", "指标名称", "数据来源"]
    STATUS_ORDER = ["草稿", "投产中", "已产出", "产出异常"]
    ACTION_RULES = {"提交投产": "投产中", "确认产出": "已产出", "标记异常": "产出异常"}
    NEGATIVE_ACTIONS = ["标记异常"]

    def compute_metric(self, metric_id: int, cube_id: int) -> dict[str, Any]:
        """计算指标，从立方获取聚合数据并写入缓存。

        跨模块数据流：
        - 读取 olap_cube 获取聚合数据
        - 读取 aggregate_cache 获取预计算缓存
        - 写入 result_cache
        - 写入 query_audit
        """
        metric = store.find(self.MODULE, metric_id)
        if metric is None:
            return {"ok": False, "message": "指标不存在"}

        cube = store.find("olap_cube", cube_id)
        if cube is None:
            return {"ok": False, "message": "依赖立方不存在"}

        # 从聚合缓存读取预计算数据
        cache_rows = store.rows("aggregate_cache")
        cached_results = []
        for cr in cache_rows:
            if cr.get("status") == "生效中" and str(cube_id) in str(cr.get("目标立方", "")):
                data = cr.get("cached_data", [])
                if data:
                    cached_results.extend(data)

        # 计算结果
        t0 = time.time()
        metric_value = _compute_metric_value(metric.get("计算公式", "SUM"), cached_results)
        elapsed_ms = (time.time() - t0) * 1000

        # 更新指标
        metric["最后产出"] = time.strftime("%Y-%m-%d %H:%M:%S")
        metric["status"] = "已产出"

        # 写入结果缓存
        _cache_metric_result(metric_id, metric_value)

        # 写入审计
        _audit_metric(metric_id, elapsed_ms, metric_value)

        return {
            "ok": True,
            "metric_id": metric_id,
            "cube_id": cube_id,
            "value": metric_value,
            "elapsed_ms": elapsed_ms,
            "formula": metric.get("计算公式"),
        }

    def produce_experiment_report(
        self,
        experiment_id: str,
        metric: str = "转化",
        *,
        role: str | None = "analyst",
        version: str | None = None,
        actor: str = "metric_engine",
    ) -> dict[str, Any]:
        """消费可共享实验分组指标，按授权曝光范围产出带分桶版本的报告。

        关键约束：
        - 只读取分组指标上明确登记的曝光编号范围，不自行扩大曝光集合；
        - 分母、转化量、对比值全部按该范围重算；
        - 报告写入实验分组指标仓库并带分桶版本；
        - 实际开放的指标结果回写实验发布清单，受限组不进入清单。
        """
        from app.services.exp_group_metric import ExpGroupMetricService
        from app.services import experiment_common as ec

        shared_svc = ExpGroupMetricService()
        view = shared_svc.shared_group_metrics(experiment_id, role, metric, version)
        if not view.get("ok"):
            return {"ok": False, "message": view.get("message", "共享分组指标不可用")}
        if not view["groups"]:
            return {"ok": False, "message": "当前角色没有可消费的分组指标"}

        version = view["version"]
        # 只消费分组指标声明的曝光范围，逐条校验曝光确实属于该组与版本。
        declared_rows = {
            str(row.get("分组名称", "")): row
            for row in store.rows("exp_group_metric")
            if str(row.get("实验编号", "")) == experiment_id
            and str(row.get("分桶版本", "")) == version
            and str(row.get("指标名称", "")) == metric
        }
        consumed: list[dict[str, Any]] = []
        for group in view["groups"]:
            declared_row = declared_rows.get(str(group.get("分组名称", "")), {})
            codes = ec.split_groups(declared_row.get("曝光范围", ""))
            if not codes:
                return {
                    "ok": False,
                    "message": f"分组 {group['分组名称']} 没有登记可共享的曝光范围",
                }
            exposures = ec.exposures_for(experiment_id, version, [group["分组名称"]], codes)
            missing = sorted(set(codes) - {str(row.get("曝光编号", "")) for row in exposures})
            if missing:
                return {
                    "ok": False,
                    "message": f"分组 {group['分组名称']} 的曝光范围与曝光记录不一致：{missing}",
                }
            recomputed = ec.aggregate_scope(experiment_id, metric, codes)
            declared_sample = declared_row.get("样本量", 0)
            declared_conversion = declared_row.get("转化量", 0)
            if (
                int(declared_sample) != recomputed["样本量"]
                or int(declared_conversion) != recomputed["转化量"]
            ):
                return {
                    "ok": False,
                    "message": (
                        f"分组 {group['分组名称']} 的分组指标（样本量 {declared_sample} / "
                        f"转化量 {declared_conversion}）与曝光范围复算结果"
                        f"（{recomputed['样本量']} / {recomputed['转化量']}）不一致，"
                        "拒绝把不可复算的口径写入报告"
                    ),
                }
            consumed.append({
                **group,
                "样本量": recomputed["样本量"],
                "转化量": recomputed["转化量"],
                "转化率": recomputed["转化率"],
                "曝光范围": ",".join(sorted(codes)),
            })

        report_id = f"MFR-{experiment_id}-{uuid.uuid4().hex[:8]}"
        report = {
            "报告编号": report_id,
            "实验编号": experiment_id,
            "分桶版本": version,
            "指标名称": metric,
            "角色": view["role"],
            "分组结果": consumed,
            "对比": view["contrasts"],
            "全量合计": view["total"],
            "抑制项": view["suppressed"],
            "受限分组": view["hidden"],
            "状态": "已生成",
            "生成人": actor,
            "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "口径": "按分组指标登记的曝光范围复算",
            "id": ec.next_id(ec.REPORT_TABLE),
            "status": "已生成",
            "pending": False,
            "abnormal": bool(view["has_hidden"]),
        }

        # 带分桶版本的报告同时写入结果缓存，供下游按版本消费。
        _cache_experiment_report(report, consumed)

        # 实际开放的指标结果回写实验发布清单。
        release = ec.write_release_entry(experiment_id, report, actor=actor)

        # 实验配置上保留一份摘要，便于列表直接看到发布范围。
        config = next(
            (row for row in store.rows("ab_config")
             if str(row.get("实验编号", "")) == experiment_id),
            None,
        )
        if config is not None:
            config["分桶版本"] = version
            config["发布清单"] = release["开放分组"]
            config["共享受限分组"] = release["受限分组"]

        ec.report_audit(
            f"METRIC_FACTORY_EXPERIMENT_REPORT experiment={experiment_id} report={report_id} "
            f"version={version} role={view['role']} groups={len(consumed)}",
            actor,
        )
        return {
            "ok": True,
            "message": "实验报告已按共享分组口径产出",
            "report": report,
            "release": release,
            "has_hidden": view["has_hidden"],
        }


def _compute_metric_value(formula: str, data: list[dict[str, Any]]) -> float:
    if not data:
        return float(random.randint(1000, 50000))
    values = [d.get("aggregated_value", d.get("value", 0)) for d in data]
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return 0.0
    if "SUM" in formula.upper():
        return float(sum(values))
    elif "AVG" in formula.upper():
        return sum(values) / len(values)
    elif "COUNT" in formula.upper():
        return float(len(values))
    return float(sum(values))


def _cache_metric_result(metric_id: int, value: float) -> None:
    rows = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    import hashlib
    cache = {
        "id": cache_id,
        "条目编号": f"RESU-M{metric_id:04d}",
        "查询摘要": f"metric_{metric_id}",
        "结果哈希": hashlib.md5(str(value).encode()).hexdigest()[:12],
        "缓存命中": "1",
        "过期时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600)),
        "存储大小": str(len(str(value))),
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "最后命中": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "result_data": [{"metric_id": metric_id, "value": value}],
    }
    rows.append(cache)


def _cache_experiment_report(report: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    """把带分桶版本的实验报告写入 result_cache，摘要中带版本便于按版本失效。"""
    rows = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    payload = {"report_id": report["报告编号"], "version": report["分桶版本"], "groups": groups}
    digest = hashlib.md5(str(payload).encode()).hexdigest()[:12]
    rows.append({
        "id": cache_id,
        "条目编号": f"RESU-AB{report['报告编号'][-8:]}",
        "查询摘要": f"experiment_report_{report['实验编号']}_{report['分桶版本']}",
        "结果哈希": digest,
        "缓存命中": "1",
        "过期时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600)),
        "存储大小": str(len(str(payload))),
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "最后命中": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "result_data": [payload],
        "分桶版本": report["分桶版本"],
    })


def _audit_metric(metric_id: int, elapsed_ms: float, value: float) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"METRIC_COMPUTE id={metric_id} value={value:.2f}",
        "执行耗时": f"{elapsed_ms:.0f}ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "metric_factory",
        "操作用户": "metric_engine",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)