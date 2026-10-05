"""端到端测试：批量纠正周期归属（reassign，v1.3）。

覆盖：
- 单条/批量（跨设备）纠正成功：仅周期归属变化，起止本地毫秒、note、
  created_at、采集编号保持不变；响应返回按目标周期锚点计算的当前定位结果
  （覆盖不足保持 unlocatable）与相关周期最新内容指纹；
- 整批拒绝且不改动数据：编号不存在、同批重复、原周期与预期不符、目标周期
  未登记（含他设备同名周期）、源/目标周期已封存、原周期等于目标周期；
- 纠正后同编号重传仍遵守同内容幂等、异内容冲突规则（周期计入内容）；
- 已发布版本保持原快照，之后发布的版本反映新归属（changes 体现位移）；
- 与封存共存：移出不可定位声段后源周期可封存；封存周期拒绝作为源/目标；
- 与版本发布并发：每个版本快照内声段归属与 UTC 投影自洽（无撕裂）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class ReassignApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def reg_cycle(self, device_id, cycle_id, note=None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def reassign(self, items):
        return self.req("POST", "/segments/reassign-cycle", {"items": items})

    def fingerprint(self, device_id, cycle_id):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def seal(self, device_id, cycle_id):
        fp = self.fingerprint(device_id, cycle_id)
        return self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                        {"fingerprint": fp})

    def get_segment(self, capture_id):
        status, body = self.req("GET", f"/segments/{capture_id}")
        self.assertEqual(status, 200)
        return body["segment"]

    def make_two_cycle_device(self, device_id="D1"):
        """default 周期斜率 1（utc=local+99000），boot-2 周期斜率 2。"""
        self.make_device(device_id)
        self.reg_cycle(device_id, "boot-2", note="第二次开机")
        self.add_anchor(device_id, 1000, 100000)
        self.add_anchor(device_id, 2000, 101000)
        self.add_anchor(device_id, 1000, 200000, "boot-2")
        self.add_anchor(device_id, 2000, 202000, "boot-2")

    # ------------------------------------------------------------ 成功路径

    def test_reassign_single_segment_success(self):
        self.make_two_cycle_device()
        _, created = self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")
        self.assertEqual(created["segment"]["utc_start_ms"], 100100)  # default 周期
        fp_default_before = self.fingerprint("D1", "default")
        fp_boot2_before = self.fingerprint("D1", "boot-2")

        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["moved"], 1)
        seg = body["segments"][0]
        # 按目标周期锚点重算：boot-2 斜率 2 -> 200200 / 200400
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 200200)
        self.assertEqual(seg["utc_end_ms"], 200400)
        # 身份字段保持不变：采集编号、设备、起止本地毫秒、备注、创建时间
        self.assertEqual(seg["capture_id"], "CAP-1")
        self.assertEqual(seg["device_id"], "D1")
        self.assertEqual(seg["cycle_id"], "boot-2")
        self.assertEqual(seg["from_cycle_id"], "default")
        self.assertEqual(seg["local_start_ms"], 1100)
        self.assertEqual(seg["local_end_ms"], 1200)
        self.assertEqual(seg["note"], "鸟鸣")
        self.assertEqual(seg["created_at"], created["segment"]["created_at"])
        # 相关周期内容指纹随之更新（与周期详情实时一致）
        cycles = {(c["device_id"], c["cycle_id"]): c for c in body["cycles"]}
        self.assertEqual(set(cycles), {("D1", "default"), ("D1", "boot-2")})
        self.assertEqual(cycles[("D1", "default")]["segment_count"], 0)
        self.assertEqual(cycles[("D1", "boot-2")]["segment_count"], 1)
        self.assertNotEqual(cycles[("D1", "default")]["fingerprint"], fp_default_before)
        self.assertNotEqual(cycles[("D1", "boot-2")]["fingerprint"], fp_boot2_before)
        self.assertEqual(cycles[("D1", "default")]["fingerprint"],
                         self.fingerprint("D1", "default"))
        self.assertEqual(cycles[("D1", "boot-2")]["fingerprint"],
                         self.fingerprint("D1", "boot-2"))

        # 当前时间轴/单条查询均反映新归属
        cur = self.get_segment("CAP-1")
        self.assertEqual(cur["cycle_id"], "boot-2")
        self.assertEqual(cur["utc_start_ms"], 200200)
        self.assertEqual(cur["created_at"], created["segment"]["created_at"])
        _, lst = self.req("GET", "/segments?device_id=D1&cycle_id=default")
        self.assertEqual(lst["count"], 0)
        _, lst = self.req("GET", "/segments?device_id=D1&cycle_id=boot-2")
        self.assertEqual([s["capture_id"] for s in lst["segments"]], ["CAP-1"])

    def test_reassign_batch_across_devices(self):
        self.make_two_cycle_device("D1")
        self.make_two_cycle_device("D2")
        self.submit("CAP-1", "D1", 1100, 1200)
        self.submit("CAP-2", "D1", 1300, 1400)
        self.submit("CAP-3", "D2", 1500, 1600)

        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-3", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-2", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["moved"], 3)
        # 响应按请求顺序返回各声段定位结果
        self.assertEqual([s["capture_id"] for s in body["segments"]],
                         ["CAP-1", "CAP-3", "CAP-2"])
        # 每项只在本设备的周期之间移动
        dev_of = {s["capture_id"]: s["device_id"] for s in body["segments"]}
        self.assertEqual(dev_of, {"CAP-1": "D1", "CAP-3": "D2", "CAP-2": "D1"})
        # 相关周期按（设备、周期）去重汇总：D1 两个周期 + D2 两个周期
        self.assertEqual(len(body["cycles"]), 4)
        for seg in body["segments"]:
            self.assertEqual(seg["status"], "located")
        cap3 = next(s for s in body["segments"] if s["capture_id"] == "CAP-3")
        self.assertEqual(cap3["utc_start_ms"], 201000)  # 200000 + 500*2

    def test_reassign_to_insufficient_coverage_stays_unlocatable(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        # boot-3 只登记周期、不录锚点 -> 覆盖不足
        self.reg_cycle("D1", "boot-3")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-3"}])
        self.assertEqual(status, 200)
        seg = body["segments"][0]
        self.assertEqual(seg["status"], "unlocatable")
        self.assertIsNone(seg["utc_start_ms"])
        self.assertIsNone(seg["utc_end_ms"])
        # 查询结果同样不可定位
        self.assertEqual(self.get_segment("CAP-1")["status"], "unlocatable")

    def test_reassign_default_cycle_shorthand(self):
        """周期字段省略时按 default 处理（与全站约定一致）。"""
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)  # 省略 cycle_id -> default
        # 省略 from_cycle_id：预期原周期 = default
        status, body = self.reassign([{"capture_id": "CAP-1", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["from_cycle_id"], "default")
        self.assertEqual(body["segments"][0]["cycle_id"], "boot-2")
        # 移回 default：省略 to_cycle_id
        status, body = self.reassign([{"capture_id": "CAP-1", "from_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["cycle_id"], "default")
        self.assertEqual(body["segments"][0]["utc_start_ms"], 100100)

    # ------------------------------------------------------------ 整批拒绝

    def test_reassign_capture_not_found(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "GHOST", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
        ])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "capture_not_found")
        self.assertEqual(body["error"]["details"]["capture_id"], "GHOST")
        # 整批拒绝：CAP-1 仍在 default，指纹不变
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_duplicate_in_batch(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "duplicate_capture_id")
        self.assertEqual(body["error"]["details"]["capture_id"], "CAP-1")
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_from_cycle_mismatch(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)  # 实际在 default
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "boot-2", "to_cycle_id": "default"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_mismatch")
        details = body["error"]["details"]
        self.assertEqual(details["capture_id"], "CAP-1")
        self.assertEqual(details["expected_from_cycle_id"], "boot-2")
        self.assertEqual(details["actual_cycle_id"], "default")
        # 状态不变
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_target_cycle_not_found(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "ghost"}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(body["error"]["details"]["cycle_id"], "ghost")
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_target_must_be_same_device_cycle(self):
        """他设备的同名周期不算已登记：只能在同一设备的周期之间移动。"""
        self.make_two_cycle_device("D1")
        self.make_device("D2")
        self.reg_cycle("D2", "boot-x")  # 周期登记在 D2 上
        self.submit("CAP-1", "D1", 1100, 1200)
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-x"}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_same_cycle_invalid(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        # 显式原周期 == 目标周期
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "default"}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "same_cycle")
        # 全部省略 -> 同为 default，同样无效
        status, body = self.reassign([{"capture_id": "CAP-1"}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "same_cycle")
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")

    def test_reassign_validation_errors(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        cases = [
            ({}, "validation_error"),                       # 缺 items
            ({"items": []}, "validation_error"),            # 空批
            ({"items": "x"}, "validation_error"),           # items 非数组
            ({"items": ["x"]}, "validation_error"),         # 项非对象
            ({"items": [{"to_cycle_id": "boot-2"}]}, "validation_error"),  # 缺编号
            ({"items": [{"capture_id": "bad id!",
                          "to_cycle_id": "boot-2"}]}, "validation_error"),
            ({"items": [{"capture_id": "CAP-1",
                          "to_cycle_id": "bad id!"}]}, "validation_error"),
        ]
        for payload, code in cases:
            status, body = self.req("POST", "/segments/reassign-cycle", payload)
            self.assertEqual(status, 400, f"payload={payload}")
            self.assertEqual(body["error"]["code"], code)
        # 均未改动数据
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    def test_reassign_atomic_rejection_leaves_batch_untouched(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        self.submit("CAP-2", "D1", 1300, 1400)
        fp_default = self.fingerprint("D1", "default")
        fp_boot2 = self.fingerprint("D1", "boot-2")
        # 第一项合法、第二项原周期与预期不符 -> 整批拒绝
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-2", "from_cycle_id": "boot-2", "to_cycle_id": "default"},
        ])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_mismatch")
        # 第一项也未生效：归属与两周期指纹均不变
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")
        self.assertEqual(self.get_segment("CAP-2")["cycle_id"], "default")
        self.assertEqual(self.fingerprint("D1", "default"), fp_default)
        self.assertEqual(self.fingerprint("D1", "boot-2"), fp_boot2)

    # ------------------------------------------------------------ 与封存共存

    def test_reassign_source_or_target_sealed_rejected(self):
        self.make_two_cycle_device()
        self.reg_cycle("D1", "boot-3")
        self.add_anchor("D1", 1000, 300000, "boot-3")
        self.add_anchor("D1", 2000, 301000, "boot-3")
        self.submit("CAP-1", "D1", 1100, 1200)              # default
        self.submit("CAP-2", "D1", 1100, 1200, "boot-2")    # boot-2
        # 封存 default 与 boot-3
        self.assertEqual(self.seal("D1", "default")[0], 201)
        self.assertEqual(self.seal("D1", "boot-3")[0], 201)
        # 源周期已封存 -> 409
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycle_id"], "default")
        # 目标周期已封存 -> 409
        status, body = self.reassign([
            {"capture_id": "CAP-2", "from_cycle_id": "boot-2", "to_cycle_id": "boot-3"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycle_id"], "boot-3")
        # 均未移动
        self.assertEqual(self.get_segment("CAP-1")["cycle_id"], "default")
        self.assertEqual(self.get_segment("CAP-2")["cycle_id"], "boot-2")

    def test_reassign_unlocatable_out_makes_cycle_sealable(self):
        self.make_two_cycle_device()
        self.submit("CAP-OK", "D1", 1100, 1200)
        self.submit("CAP-OUT", "D1", 9000, 9500)  # 越界 -> 不可定位
        # 存在不可定位声段 -> 不可封存
        status, body = self.seal("D1", "default")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "seal_unlocatable_segments")
        stale_fp = self.fingerprint("D1", "default")
        # 把不可定位声段移到 boot-2（目标周期覆盖不足，保持不可定位）
        status, body = self.reassign([
            {"capture_id": "CAP-OUT", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["status"], "unlocatable")
        # 旧指纹已过期
        status, body = self.req("POST", "/devices/D1/cycles/default/seal",
                                {"fingerprint": stale_fp})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "seal_fingerprint_stale")
        # 新指纹可封存（剩余声段全部可定位）
        self.assertEqual(self.seal("D1", "default")[0], 201)

    # ------------------------------------------------------------ 重传规则与版本

    def test_reassign_then_resubmit_keeps_idempotent_conflict_rules(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")
        self.assertEqual(self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default",
             "to_cycle_id": "boot-2"}])[0], 200)
        # 按新归属同内容重传 -> 幂等
        status, body = self.submit("CAP-1", "D1", 1100, 1200, "boot-2", note="鸟鸣")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["segment"]["utc_start_ms"], 200200)
        # 按旧周期重传 -> 周期计入内容，异内容冲突
        status, body = self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["cycle_id"], "boot-2")
        # 仍只有一条
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    def test_reassign_versions_reflect_new_attribution(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200)
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v1["segments"][0]["cycle_id"], "default")
        self.assertEqual(v1["segments"][0]["utc_start_ms"], 100100)

        self.assertEqual(self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default",
             "to_cycle_id": "boot-2"}])[0], 200)

        # 之后发布的版本反映新归属，changes 体现位移
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["segments"][0]["cycle_id"], "boot-2")
        self.assertEqual(v2["segments"][0]["utc_start_ms"], 200200)
        disp = v2["changes"]["displaced"]
        self.assertEqual([d["capture_id"] for d in disp], ["CAP-1"])
        self.assertEqual(disp[0]["previous"]["utc_start_ms"], 100100)
        self.assertEqual(disp[0]["current"]["utc_start_ms"], 200200)
        self.assertEqual(disp[0]["start_shift_ms"], 100100)
        # 已发布版本保持原快照
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again["segments"], v1["segments"])
        self.assertEqual(v1_again["segments"][0]["cycle_id"], "default")

    # ------------------------------------------------------------ 并发一致性

    def test_reassign_concurrent_with_publish(self):
        """批量纠正与发布并发：每个版本快照内声段归属与 UTC 投影自洽。"""
        self.make_two_cycle_device()
        for i in range(4):
            self.submit(f"S{i}", "D1", 1100 + i * 10, 1150 + i * 10)

        results, errors = [], []

        def move(from_cycle, to_cycle):
            try:
                items = [{"capture_id": f"S{i}", "from_cycle_id": from_cycle,
                          "to_cycle_id": to_cycle} for i in range(4)]
                # 200=生效；409=与另一批纠正串行后原周期不符（预期内）
                results.append(self.reassign(items)[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for _ in range(4):
            threads.append(threading.Thread(target=move, args=("default", "boot-2")))
            threads.append(threading.Thread(target=move, args=("boot-2", "default")))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201, 409) for s in results))

        _, body = self.req("GET", "/versions")
        versions = body["versions"]
        self.assertGreaterEqual(len(versions), 1)
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            # 快照完整：4 个声段都在，归属只能是两个周期之一
            self.assertEqual(doc["segment_count"], 4)
            for seg in doc["segments"]:
                self.assertEqual(seg["status"], "located")
                if seg["cycle_id"] == "default":   # 斜率 1：utc = local + 99000
                    self.assertEqual(seg["utc_start_ms"],
                                     seg["local_start_ms"] + 99000)
                else:                              # boot-2 斜率 2
                    self.assertEqual(seg["cycle_id"], "boot-2")
                    self.assertEqual(seg["utc_start_ms"],
                                     200000 + (seg["local_start_ms"] - 1000) * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
