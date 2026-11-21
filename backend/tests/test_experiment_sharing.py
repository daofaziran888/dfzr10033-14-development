"""实验共享分组指标、共享报告与指标工厂回写的集成测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services import experiment_common as ec
from app.services.exp_group_metric import ExpGroupMetricService
from app.services.metric_factory import MetricFactoryService
from app.store import store

EXPERIMENT_TABLES = [
    "ab_config", ec.BUCKET_TABLE, ec.EXPOSURE_TABLE, ec.DETAIL_TABLE,
    ec.GROUP_METRIC_TABLE, ec.SHARE_TABLE, ec.ACCESS_TABLE,
    ec.REPORT_TABLE, ec.DELIVERY_TABLE, ec.RELEASE_TABLE,
    "result_cache",
]


@pytest.fixture(autouse=True)
def reset_experiment_tables():
    """每个用例从种子快照重建实验相关表，避免共享范围调整污染其他用例。"""
    original = {
        name: [dict(row) for row in SEED_ROWS.get(name, [])]
        for name in EXPERIMENT_TABLES
    }
    current = {name: [dict(row) for row in store.rows(name)] for name in EXPERIMENT_TABLES}
    yield
    for name, rows in original.items():
        store._tables[name] = rows
    # result_cache 在用例里可能被指标工厂追加，恢复为用例开始时的内容。
    if "result_cache" in current:
        store._tables["result_cache"] = current["result_cache"]


@pytest.fixture()
def svc():
    return ExpGroupMetricService()


@pytest.fixture()
def factory():
    return MetricFactoryService()


def _codes(experiment_id: str, version: str, group: str) -> list[str]:
    return ec.exposure_ids(ec.exposures_for(experiment_id, version, [group]))


class TestExposureLocking:
    def test_exposure_locks_bucket_version(self, svc):
        result = svc.record_exposure(
            "AB_EXP_0001", "u-lock-1", exposure_code="EXP-LOCK-1",
            sample_size=20, converted=5,
        )
        assert result["ok"]
        exposure = result["exposure"]
        assert exposure["分桶版本"] == "v1"
        assert exposure["锁定版本"] == "v1"
        assert exposure["分组名称"] in {"control", "treatment"}

    def test_same_exposure_cannot_move_group(self, svc):
        first = svc.record_exposure(
            "AB_EXP_0001", "u-lock-2", exposure_code="EXP-LOCK-2", sample_size=5,
        )
        assert first["ok"]
        locked = first["group"]
        other = "control" if locked == "treatment" else "treatment"
        # 直接改仓库里曝光的组，模拟上游错误重放，再次写入必须被拒绝。
        exposure = first["exposure"]
        exposure["分组名称"] = other
        replay = svc.record_exposure(
            "AB_EXP_0001", "u-lock-2", exposure_code="EXP-LOCK-2", sample_size=5,
        )
        assert not replay["ok"]
        assert "不能改归" in replay["message"]

    def test_user_buckets_to_single_group_per_version(self, svc):
        for index in range(30):
            result = svc.record_exposure("AB_EXP_0001", f"bucket-user-{index}")
            assert result["ok"]
            assert result["exposure"]["分桶版本"] == "v1"
        groups = {
            row["分组名称"]
            for row in ec.exposures_for("AB_EXP_0001", "v1")
            if row["用户标识"].startswith("bucket-user-")
        }
        assert groups <= {"control", "treatment"}


class TestGroupMetricAggregation:
    def test_aggregation_uses_declared_exposure_scope(self, svc):
        result = svc.compute_group_metrics("AB_EXP_0002", "转化", "v1")
        assert result["ok"]
        rows = {row["分组名称"]: row for row in result["groups"]}
        assert set(rows) == {"control", "treatment"}
        control = rows["control"]
        # 分组指标保存的是明确的曝光编号范围，而不是模糊筛选条件。
        assert control["曝光范围"] == "EXP-AB_EXP_0002-v1-C1,EXP-AB_EXP_0002-v1-C2"
        assert int(control["样本量"]) == 2000
        assert int(control["转化量"]) == 170
        assert control["转化率"] == "0.085000"

    def test_exposure_in_unknown_group_rejected(self, svc):
        store.rows(ec.EXPOSURE_TABLE).append({
            "id": 9999, "曝光编号": "EXP-BAD-1", "实验编号": "AB_EXP_0002",
            "用户标识": "u-bad", "分组名称": "ghost", "分桶版本": "v1",
            "样本数": "1", "转化数": "0", "status": "已曝光",
        })
        result = svc.compute_group_metrics("AB_EXP_0002", "转化", "v1")
        assert not result["ok"]
        assert "不在分桶版本" in result["message"]


class TestRoleScopedReads:
    def test_public_role_cannot_see_restricted_group(self, svc):
        view = svc.shared_group_metrics("AB_EXP_0002", "analyst", "转化", "v1")
        assert view["ok"]
        assert [row["分组名称"] for row in view["groups"]] == ["control"]
        assert view["total"] is None, "存在未授权组时不得返回全量合计"
        assert view["contrasts"] == []
        names = {item["分组名称"] for item in view["hidden"]}
        assert "treatment" in names
        serialized = str(view)
        assert "270" not in serialized, "受限组转化量不能出现在返回体里"

    def test_restricted_role_sees_both_groups(self, svc):
        view = svc.shared_group_metrics("AB_EXP_0002", "analyst_restricted", "转化", "v1")
        assert view["ok"]
        assert {row["分组名称"] for row in view["groups"]} == {"control", "treatment"}
        assert view["total"]["样本量"] == 4000
        assert view["contrasts"][0]["相对提升"] is not None

    def test_generic_list_hides_restricted_rows(self, svc):
        rows, total = svc.list_entries(role="analyst")
        assert total == len(rows)
        assert all(row["共享范围"] == "公开" for row in rows)
        assert svc.get_entry(4) is None, "受限组单条读取应按不可见处理"


class TestGroupShareAdjustment:
    def test_exit_share_marks_deliveries_and_new_reports_recompute(self, svc):
        draft = svc.prepare_shared_report("AB_EXP_0001", "analyst")
        assert draft["ok"]
        report_id = draft["report"]["报告编号"]
        published = svc.publish_report("AB_EXP_0001", report_id)
        assert published["ok"]
        delivery = published["delivery"]
        assert {"control", "treatment"} <= set(ec.split_groups(delivery["可见分组"]))

        adjusted = svc.set_group_share("AB_EXP_0001", "treatment", "已退出")
        assert adjusted["ok"]
        assert adjusted["before"] == "公开"
        assert adjusted["after"] == "已退出"
        recalled = [item for item in adjusted["recalled_deliveries"]
                    if item["报告编号"] == report_id]
        assert recalled and recalled[0]["状态"] == "待回收"

        # 新报告和读取都按退出后的授权范围复算。
        view = svc.shared_group_metrics("AB_EXP_0001", "analyst_restricted", "转化", "v1")
        assert [row["分组名称"] for row in view["groups"]] == ["control"]
        assert view["total"] is None
        new_report = svc.prepare_shared_report("AB_EXP_0001", "analyst_restricted")
        assert [row["分组名称"] for row in new_report["report"]["分组结果"]] == ["control"]

        # 已发布的旧交付保持历史快照，不被原地改写。
        old_delivery = next(
            item for item in svc.list_deliveries(report_id=report_id)
        )
        assert "treatment" in old_delivery["可见分组"]


class TestPairedPublishConflict:
    def test_group_exits_between_draft_and_publish(self, svc):
        draft = svc.prepare_shared_report("AB_EXP_0001", "analyst")
        report_id = draft["report"]["报告编号"]
        assert {row["分组名称"] for row in draft["report"]["分组结果"]} == {"control", "treatment"}

        svc.set_group_share("AB_EXP_0001", "treatment", "已退出")

        blocked = svc.publish_report("AB_EXP_0001", report_id)
        assert blocked["ok"] is False and blocked["conflict"] is True
        assert blocked["report"]["status"] == "待复核"
        assert "成对分析" in blocked["message"]
        assert svc.list_deliveries(report_id=report_id) == []

        partial = svc.publish_report("AB_EXP_0001", report_id, allow_partial=True)
        assert partial["ok"]
        report = partial["report"]
        assert report["视图范围"] == "可见组"
        assert [row["分组名称"] for row in report["分组结果"]] == ["control"]
        assert report["全量合计"] is None
        assert report["对比"] == []
        restricted = [item["分组名称"] for item in report["受限分组"]]
        assert "treatment" in restricted

        # 用受限组的真实数值做差反推：可见小计 + 未知 ≠ 全量，报告没有可用的全量。
        control_sample = report["分组结果"][0]["样本量"]
        treatment_sample = int(next(
            row["样本量"] for row in store.rows(ec.GROUP_METRIC_TABLE)
            if row["分组指标编号"] == "EGM-AB_EXP_0001-v1-treatment"
        ))
        assert report["全量合计"] is None
        assert control_sample + treatment_sample == 4000

    def test_cannot_publish_same_report_twice(self, svc):
        draft = svc.prepare_shared_report("AB_EXP_0001", "experiment_owner")
        report_id = draft["report"]["报告编号"]
        assert svc.publish_report("AB_EXP_0001", report_id)["ok"]
        second = svc.publish_report("AB_EXP_0001", report_id)
        assert not second["ok"]
        assert "不能就地改写" in second["message"]


class TestMetricFactoryPipeline:
    def test_factory_consumes_shared_scope_and_writes_release(self, factory):
        result = factory.produce_experiment_report("AB_EXP_0002", "转化", role="analyst")
        assert result["ok"]
        report = result["report"]
        assert report["分桶版本"] == "v1"
        assert [row["分组名称"] for row in report["分组结果"]] == ["control"]
        assert report["全量合计"] is None

        release = result["release"]
        assert release["分桶版本"] == "v1"
        assert release["开放分组"] == "control"
        assert release["受限分组"] == "treatment"
        release_values = release["开放指标"]
        assert "'样本量': 2000" in release_values or '"样本量": 2000' in release_values
        assert "270" not in release_values, "发布清单不能带入受限组转化量"

        cached = [row for row in store.rows("result_cache")
                  if row["查询摘要"] == "experiment_report_AB_EXP_0002_v1"]
        assert cached and cached[-1]["分桶版本"] == "v1"

    def test_factory_rejects_non_recomputable_metric(self, factory):
        row = next(
            item for item in store.rows(ec.GROUP_METRIC_TABLE)
            if item["分组指标编号"] == "EGM-AB_EXP_0002-v1-control"
        )
        row["样本量"] = "9999"
        result = factory.produce_experiment_report("AB_EXP_0002", "转化", role="metric_engine")
        assert not result["ok"]
        assert "不可复算" in result["message"]

    def test_api_pipeline_and_conflict_status(self):
        client = TestClient(app)
        resp = client.post("/api/pipelines/experiment-shared-report",
                           params={"experiment_id": "AB_EXP_0002", "role": "analyst"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] and payload["release"]["开放分组"] == "control"

        draft = client.post("/api/experiments/AB_EXP_0001/reports",
                            params={"role": "analyst_restricted"}).json()["report"]
        assert {row["分组名称"] for row in draft["分组结果"]} == {"control", "treatment"}
        client.post("/api/experiments/AB_EXP_0001/group-share",
                    params={"group": "treatment", "share": "已退出"})
        conflict = client.post(
            f"/api/experiments/AB_EXP_0001/reports/{draft['报告编号']}/publish")
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["conflict"] is True
        assert detail["report"]["status"] == "待复核"

        partial = client.post(
            f"/api/experiments/AB_EXP_0001/reports/{draft['报告编号']}/publish",
            params={"allow_partial": True})
        assert partial.status_code == 200
        assert partial.json()["report"]["视图范围"] == "可见组"


class TestSharedReportPermission:
    def test_non_publisher_cannot_publish(self, svc):
        draft = svc.prepare_shared_report("AB_EXP_0001", "analyst")
        result = svc.publish_report(
            "AB_EXP_0001", draft["report"]["报告编号"], actor="analyst",
        )
        assert result["ok"] is False
        assert "无权发布" in result["message"]
        assert svc.list_deliveries(experiment_id="AB_EXP_0001") == []

    def test_group_exit_recalls_existing_reader_delivery(self, svc):
        draft = svc.prepare_shared_report("AB_EXP_0001", "analyst")
        published = svc.publish_report("AB_EXP_0001", draft["report"]["报告编号"])
        delivery_id = published["delivery"]["交付编号"]

        adjusted = svc.set_group_share("AB_EXP_0001", "treatment", "已退出")
        recalled = {item["交付编号"] for item in adjusted["recalled_deliveries"]}
        assert delivery_id in recalled
        delivery = next(
            row for row in svc.list_deliveries(experiment_id="AB_EXP_0001")
            if row["交付编号"] == delivery_id
        )
        assert delivery["status"] == "待回收"
        assert delivery["abnormal"] is True

        # 读者再次读取时，受限组的样本量与转化量都不出现。
        view = svc.shared_group_metrics("AB_EXP_0001", "analyst_restricted", "转化", "v1")
        assert view["total"] is None
        assert "320" not in str(view["groups"])
        remaining = svc.list_deliveries(experiment_id="AB_EXP_0001", role="analyst")
        assert remaining and remaining[0]["视图范围"] == "全部"
