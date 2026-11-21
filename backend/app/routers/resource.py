"""统一资源接口：CRUD + 跨模块管线 API。"""
from __future__ import annotations

import csv
import io
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


# -- CRUD --

@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str, request: Request,
    keyword: str | None = Query(default=None), status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {k: v.strip() for k, v in request.query_params.items()
               if k in service.LIST_FIELDS and v.strip()}
    items, total = service.list_entries(filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str, request: Request,
    keyword: str | None = Query(default=None), status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {k: v.strip() for k, v in request.query_params.items()
               if k in service.LIST_FIELDS and v.strip()}
    items, _ = service.list_entries(filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(f, "") for f in service.LIST_FIELDS), item.get("status", "")]
        writer.writerow(["'" + str(v) if str(v or "").startswith(("=", "+", "-", "@")) else v for v in values])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{module}.csv"'})


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


# -- 8 条跨模块管道 API --

from app.services.source_registry import SourceRegistryService
from app.services.query_federation import QueryFederationService
from app.services.olap_cube import OlapCubeService
from app.services.data_exporter import DataExporterService
from app.services.result_cache import ResultCacheService
from app.services.aggregate_cache import AggregateCacheService
from app.services.data_lake_read import DataLakeReadService
from app.services.query_rewrite import QueryRewriteService
from app.services.query_audit import QueryAuditService
from app.services.cost_govern import CostGovernService
from app.services.data_contract import DataContractService
from app.services.semantic_model import SemanticModelService

source_svc = SourceRegistryService()
semantic_svc = SemanticModelService()
federation_svc = QueryFederationService()
cube_svc = OlapCubeService()
exporter_svc = DataExporterService()
result_cache_svc = ResultCacheService()
agg_cache_svc = AggregateCacheService()
lake_svc = DataLakeReadService()
rewrite_svc = QueryRewriteService()
audit_svc = QueryAuditService()
cost_svc = CostGovernService()
contract_svc = DataContractService()


@router.post("/pipelines/source-to-federated")
def pipeline_source_to_federated(
    source_no: str = Query(...), source_type: str = Query(default="mysql"),
    source_addr: str = Query(default="db://demo/mydb"),
) -> dict[str, Any]:
    entry, msg = source_svc.register_source({
        "源编号": source_no, "源类型": source_type, "连接地址": source_addr,
        "驱动配置": f"{source_type}_driver", "数据库名": "demo_db", "认证方式": "token"})
    if entry is None:
        raise HTTPException(status_code=400, detail=msg)
    sid = entry["id"]
    schema_r = source_svc.detect_schema_changes(sid)
    plan = federation_svc.build_federation_plan([source_no])
    fe, missing = federation_svc.create_entry({
        "计划编号": f"QUER-P{sid:04d}", "参与来源": source_no,
        "关联键": plan["关联键"], "下推策略": plan["下推策略"], "数据量估算": plan["数据量估算"],
        "并行度": plan["并行度"], "超时上限": plan["超时上限"], "结果合并": plan["结果合并"]})
    if fe is None:
        raise HTTPException(status_code=400, detail=f"创建计划失败: {missing}")
    exec_r = federation_svc.execute_federation_query(fe["id"])
    return {"ok": True, "pipeline": "source_to_federated", "source": {"id": sid, "no": source_no},
            "schema": schema_r, "federation_plan": plan, "execution": exec_r}


@router.post("/pipelines/cube-build-cache")
def pipeline_cube_build_cache(
    cube_no: str = Query(...), cube_name: str = Query(...),
    dims: str = Query(default="region,category"), measures: str = Query(default="revenue,orders"),
    agg_type: str = Query(default="SUM"),
) -> dict[str, Any]:
    entry, missing = cube_svc.create_entry({
        "立方编号": cube_no, "立方名称": cube_name, "维度列表": dims, "度量列表": measures,
        "聚合类型": agg_type, "分区策略": "dt", "刷新周期": "3600", "数据行数": "0"})
    if entry is None:
        raise HTTPException(status_code=400, detail=f"创建立方失败: {missing}")
    cid = entry["id"]
    return {"ok": True, "pipeline": "cube_build_cache", "cube_id": cid,
            "build": cube_svc.build_cube(cid),
            "prewarm": agg_cache_svc.prewarm_cube_cache(cid),
            "consistency": agg_cache_svc.check_cache_consistency(cid)}


@router.post("/pipelines/rewrite-estimate-execute")
def pipeline_rewrite_estimate_execute(
    query: str = Query(default="SELECT * FROM source_registry"),
    model_ids: str = Query(default=""),
) -> dict[str, Any]:
    mids = [int(m) for m in model_ids.split(",") if m.strip()] if model_ids else None
    rr = rewrite_svc.rewrite_query(query, mids)
    cr = federation_svc.estimate_query_cost(["source_registry"], "SELECT")
    return {"ok": True, "pipeline": "rewrite_estimate_execute", "rewrite": rr, "cost_estimate": cr,
            "execution_summary": {"original": query, "rewritten": rr["rewritten"],
                                  "rules_applied": rr["rules_applied"],
                                  "estimated_savings": rr["estimated_savings_rows"]}}


@router.post("/pipelines/query-audit-profile")
def pipeline_query_audit_profile(
    query_stmt: str = Query(default="FEDERATED_QUERY plan_id=1"),
) -> dict[str, Any]:
    audits = store.rows("query_audit")
    slow = audit_svc.analyze_slow_queries(threshold_ms=1000)
    profile = audit_svc.get_query_profile(audits[-1]["id"]) if audits else None
    report = audit_svc.generate_performance_report(time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d"))
    return {"ok": True, "pipeline": "query_audit_profile", "slow_analysis": slow,
            "profile": profile, "report": report}


@router.post("/pipelines/schema-drift-detect")
def pipeline_schema_drift_detect(source_id: int = Query(...)) -> dict[str, Any]:
    schema_r = source_svc.detect_schema_changes(source_id)
    conflicts = semantic_svc.detect_schema_conflicts(source_id)
    affected = [{"contract_id": c["id"], "消费者": c.get("消费者"),
                 "violations": contract_svc.verify_contract_compliance(c["id"]).get("violations", [])}
                for c in store.rows("data_contract")
                if c.get("status") == "已承诺" and contract_svc.verify_contract_compliance(c["id"]).get("violations")]
    return {"ok": True, "pipeline": "schema_drift_detect", "source_id": source_id,
            "schema_changes": schema_r, "model_conflicts": conflicts, "affected_contracts": affected}


@router.post("/pipelines/export-to-mart")
def pipeline_export_to_mart(
    export_no: str = Query(...), fmt: str = Query(default="csv"), mart_id: int = Query(default=0),
) -> dict[str, Any]:
    entry, missing = exporter_svc.create_entry({
        "任务编号": export_no, "数据来源": "result_cache,result_mart", "筛选条件": "all",
        "输出格式": fmt, "字段列表": "*", "排序规则": "id", "导出上限": "10000",
        "接收地址": "local://exports"})
    if entry is None:
        raise HTTPException(status_code=400, detail=f"创建导出任务失败: {missing}")
    return {"ok": True, "pipeline": "export_to_mart",
            "export": exporter_svc.export_query_results(entry["id"]),
            "mart": exporter_svc.batch_export_to_mart(mart_id) if mart_id > 0 else None}


@router.post("/pipelines/lake-scan-register")
def pipeline_lake_scan_register(path_id: int = Query(...)) -> dict[str, Any]:
    return {"ok": True, "pipeline": "lake_scan_register",
            "scan": lake_svc.scan_and_register(path_id),
            "drift": lake_svc.detect_schema_drift(path_id),
            "cost": lake_svc.estimate_partition_scan_cost(path_id, ("2026-11-01", "2026-11-17"))}


@router.post("/pipelines/cost-aggregate-alert")
def pipeline_cost_aggregate_alert() -> dict[str, Any]:
    return {"ok": True, "pipeline": "cost_aggregate_alert",
            "cost_aggregation": cost_svc.aggregate_module_costs(),
            "quota_check": cost_svc.check_monthly_quota(),
            "cache_report": result_cache_svc.cache_hit_rate_report()}


# -- 实验共享分组指标：曝光锁定、按角色读取、共享报告与发布清单 --

from app.services.exp_group_metric import ExpGroupMetricService
from app.services.metric_factory import MetricFactoryService
from app.services import experiment_common as exp_common

exp_metric_svc = ExpGroupMetricService()
metric_factory_svc = MetricFactoryService()


@router.post("/experiments/{experiment_id}/exposures")
def record_experiment_exposure(
    experiment_id: str,
    user_id: str = Query(...),
    exposure_code: str | None = Query(default=None),
    version: str | None = Query(default=None),
    sample_size: int = Query(default=1, ge=0),
    converted: int = Query(default=0, ge=0),
    metric: str = Query(default="转化"),
) -> dict[str, Any]:
    """写入曝光：分桶版本随即锁定，同一条曝光只归一个实际组。"""
    return exp_metric_svc.record_exposure(
        experiment_id, user_id, exposure_code=exposure_code, version=version,
        sample_size=sample_size, converted=converted, metric=metric,
    )


@router.get("/experiments/{experiment_id}/group-metrics")
def shared_experiment_group_metrics(
    experiment_id: str,
    role: str = Query(default="analyst"),
    metric: str = Query(default="转化"),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """数据分析人员读取自己可访问的分组指标；不可见组不返回数值。"""
    if not exp_common.experiment_exists(experiment_id):
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 不存在")
    result = exp_metric_svc.shared_group_metrics(experiment_id, role, metric, version)
    if not result.get("ok") and "没有任何可读分组" in str(result.get("message", "")):
        raise HTTPException(status_code=403, detail=result["message"])
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "分组指标不可用"))
    return result


@router.post("/experiments/{experiment_id}/group-metrics/compute")
def compute_experiment_group_metrics(
    experiment_id: str,
    role: str = Query(default="metric_engine"),
    metric: str = Query(default="转化"),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """内部聚合：关联分桶配置与指标明细，按版本保存每组明确的曝光范围。"""
    if not exp_common.experiment_exists(experiment_id):
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 不存在")
    result = exp_metric_svc.compute_group_metrics(
        experiment_id, metric, version, actor_role=role,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403 if "无权" in str(result.get("message")) else 400,
                            detail=result.get("message"))
    return result


@router.post("/experiments/{experiment_id}/group-share")
def adjust_experiment_group_share(
    experiment_id: str,
    group: str = Query(...),
    share: str = Query(...),
    version: str | None = Query(default=None),
    actor: str = Query(default="experiment_owner"),
) -> dict[str, Any]:
    """调整分组共享归属；返回受影响的报告和读者交付记录供核对。"""
    result = exp_metric_svc.set_group_share(
        experiment_id, group, share, actor=actor, version=version,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/experiments/{experiment_id}/reports")
def prepare_experiment_shared_report(
    experiment_id: str,
    role: str = Query(default="analyst"),
    metric: str = Query(default="转化"),
    version: str | None = Query(default=None),
    created_by: str = Query(default="experiment_owner"),
) -> dict[str, Any]:
    """生成共享只读报告草稿：只覆盖角色授权范围，并标明被抑制的合计与对比。"""
    if not exp_common.experiment_exists(experiment_id):
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 不存在")
    result = exp_metric_svc.prepare_shared_report(
        experiment_id, role, metric=metric, version=version, created_by=created_by,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403 if "没有任何可读分组" in str(result.get("message", ""))
                            else 400, detail=result.get("message"))
    return result


@router.get("/experiments/{experiment_id}/reports")
def list_experiment_reports(
    experiment_id: str,
    role: str | None = Query(default=None),
) -> dict[str, Any]:
    reports = exp_metric_svc.list_reports(experiment_id, role)
    return {"items": reports, "total": len(reports)}


@router.post("/experiments/{experiment_id}/reports/{report_id}/publish")
def publish_experiment_shared_report(
    experiment_id: str,
    report_id: str,
    actor: str = Query(default="experiment_owner"),
    publish_mode: str = Query(default="paired"),
    allow_partial: bool = Query(default=False),
) -> dict[str, Any]:
    """发布共享只读报告：发布前按实际授权范围复算，成对分析冲突时挂起待复核。"""
    result = exp_metric_svc.publish_report(
        experiment_id, report_id, actor=actor,
        publish_mode=publish_mode, allow_partial=allow_partial,
    )
    if result.get("conflict"):
        # 冲突不是服务故障：以 409 返回待复核状态和新报告快照。
        raise HTTPException(status_code=409, detail=result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/experiments/{experiment_id}/deliveries")
def list_experiment_deliveries(
    experiment_id: str,
    role: str | None = Query(default=None),
) -> dict[str, Any]:
    deliveries = exp_metric_svc.list_deliveries(experiment_id=experiment_id, role=role)
    return {"items": deliveries, "total": len(deliveries)}


@router.get("/experiments/{experiment_id}/release")
def get_experiment_release(experiment_id: str) -> dict[str, Any]:
    releases = exp_metric_svc.list_releases(experiment_id)
    if not releases:
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 还没有发布清单")
    return releases[0]


@router.post("/pipelines/experiment-shared-report")
def pipeline_experiment_shared_report(
    experiment_id: str = Query(...),
    metric: str = Query(default="转化"),
    role: str = Query(default="analyst"),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """指标工厂消费可共享实验分组指标，产出带分桶版本的报告并回写发布清单。"""
    if not exp_common.experiment_exists(experiment_id):
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 不存在")
    result = metric_factory_svc.produce_experiment_report(
        experiment_id, metric, role=role, version=version,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return {"ok": True, "pipeline": "experiment_shared_report", **result}