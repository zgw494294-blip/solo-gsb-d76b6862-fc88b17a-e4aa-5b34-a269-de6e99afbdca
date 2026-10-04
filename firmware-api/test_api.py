"""端到端测试：用 FastAPI TestClient 覆盖全部需求场景。

运行：python test_api.py
"""
import base64
import importlib
import os
import sqlite3
import sys
import tempfile
import threading

TMP = tempfile.mkdtemp(prefix="fwapi-")
os.environ["FIRMWARE_DB_PATH"] = os.path.join(TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app.main as m  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

m.init_db()  # 生产环境由 lifespan 调用；此处避免 TestClient 未进上下文

PASS = 0


def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"  ok - {name}")


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def new_client():
    return TestClient(m.app)


client = new_client()

print("== 1. 创建设备：初始镜像确认入库，限制 64 KiB ==")
fw_v1 = bytes(range(256)) * 4  # 1024 字节
r = client.post("/devices", json={
    "device_id": "dev1",
    "initial_image_base64": b64(fw_v1),
    "initial_sha256": __import__("hashlib").sha256(fw_v1).hexdigest(),
})
check("create 201", r.status_code == 201, r.status_code)
st = r.json()
check("initial confirmed slot0", st["confirmed_slot"] == 0 and st["running_slot"] == 0)
check("slot0 confirmed w/ digest",
      st["slots"]["slot0"]["status"] == "confirmed"
      and st["slots"]["slot0"]["image_sha256"] == __import__("hashlib").sha256(fw_v1).hexdigest())
check("slot1 empty", st["slots"]["slot1"]["status"] == "empty"
      and st["pending_token"] is None)

r = client.post("/devices", json={"device_id": "dev1", "initial_image_base64": b64(b"x")})
check("duplicate device 409", r.status_code == 409)

big = bytes(64 * 1024 + 1)
r = client.post("/devices", json={"device_id": "dev-big", "initial_image_base64": b64(big)})
check("image >64KiB 422", r.status_code == 422, r.status_code)

r = client.post("/devices", json={"device_id": "dev-empty", "initial_image_base64": ""})
check("empty image 422", r.status_code == 422)

r = client.post("/devices", json={
    "device_id": "dev-badsum", "initial_image_base64": b64(b"abc"),
    "initial_sha256": "00" * 32})
check("wrong initial sha 422", r.status_code == 422)

r = client.post("/devices", json={
    "device_id": "dev-badsum2", "initial_image_base64": b64(b"abc"),
    "initial_sha256": "not-hex"})
check("malformed initial sha 422", r.status_code == 422)

r = client.put("/devices/dev1/update", json={"length": 10, "sha256": "zz" * 32})
check("malformed declare sha 422", r.status_code == 422)

r = client.post("/devices", json={"device_id": "dev-b64", "initial_image_base64": "not base64!!"})
check("bad base64 422", r.status_code == 422)

r = client.get("/devices/nope")
check("unknown device 404", r.status_code == 404)

print("== 2. 声明更新只写另一槽 ==")
fw_v2 = b"FW2-" + bytes((i * 7 % 256 for i in range(30716)))  # 30720 字节
import hashlib
sha_v2 = hashlib.sha256(fw_v2).hexdigest()

r = client.put("/devices/dev1/update", json={"length": len(fw_v2), "sha256": sha_v2})
check("declare 200", r.status_code == 200, r.status_code)
s1 = r.json()["slots"]["slot1"]
check("slot1 in-progress", s1["status"] == "in-progress"
      and s1["declared_length"] == 30720 and s1["bytes_received"] == 0)
# 未声明设备直接发分块
client.post("/devices", json={"device_id": "dev2", "initial_image_base64": b64(b"hello")})
r = client.put("/devices/dev2/chunks", json={"offset": 0, "data_base64": b64(b"xx")})
check("chunk without declaration 409", r.status_code == 409)

print("== 3. 分块：重传幂等 / 重叠冲突 / 越界 ==")
def put_chunk(off, data):
    return client.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(data)})

r = put_chunk(0, fw_v2[:4096])
check("chunk0 accepted", r.status_code == 200 and r.json()["candidate_status"] == "in-progress")
r = put_chunk(0, fw_v2[:4096])
check("identical redelivery 200", r.status_code == 200
      and r.json()["identical_redelivery"] is True)
# 同偏移、前缀相同但短 1 字节：重叠区字节一致（仅更短），允许
r = put_chunk(0, fw_v2[:4095])
check("same prefix shorter chunk allowed", r.status_code == 200
      and r.json()["identical_redelivery"] is False)
# 同偏移、字节不同（重叠区冲突）
bad = bytearray(fw_v2[:4096]); bad[100] ^= 0xFF
r = put_chunk(0, bytes(bad))
check("same offset diff content 409", r.status_code == 409
      and r.json()["detail"]["code"] == "overlap_conflict")
# 部分重叠内容不同
bad2 = bytearray(fw_v2[4090:4110]); bad2[3] ^= 0xFF
r = put_chunk(4090, bytes(bad2))
check("overlapping diff bytes 409", r.status_code == 409)
# 部分重叠内容相同（幂等扩展）
r = put_chunk(4090, fw_v2[4090:4110])
check("overlapping same bytes ok", r.status_code == 200)
# 越界（放在未覆盖区，单纯超声明末端）
r = put_chunk(30719, fw_v2[30719:30720] + b"X")
check("chunk past declared end 422", r.status_code == 422)

print("== 4. 不完整不能启动；摘要错误不能启动，旧镜像仍启动 ==")
r = client.post("/devices/dev1/restart")
j = r.json()
check("incomplete -> old image boots", r.status_code == 200
      and j["result"] == "confirmed" and j["running_slot"] == 0 and j["boot_token"] is None)
st = client.get("/devices/dev1").json()
check("still running confirmed slot0", st["running_slot"] == 0
      and st["pending_token"] is None)

# 用错误摘要声明：传完应得 digest-mismatch
fw_bad = bytes(range(100))
client.put("/devices/dev1/update", json={"length": 100, "sha256": "11" * 32})
r = put_chunk(0, fw_bad)
check("full upload wrong digest -> digest-mismatch",
      r.json()["candidate_status"] == "digest-mismatch" and r.json()["bootable"] is False)
r = client.post("/devices/dev1/restart")
j = r.json()
check("digest-mismatch -> old boots, candidate discarded",
      j["result"] == "confirmed" and j["running_slot"] == 0)
st = client.get("/devices/dev1").json()
check("discarded slot empty", st["slots"]["slot1"]["status"] == "empty"
      and st["slots"]["slot1"]["declared_length"] is None)

print("== 5. 完整且摘要相符 -> 试启，令牌持久化后返回 ==")
client.put("/devices/dev1/update", json={"length": len(fw_v2), "sha256": sha_v2})
for off in range(0, len(fw_v2), 4096):
    r = put_chunk(off, fw_v2[off:off + 4096])
check("last chunk -> complete bootable",
      r.json()["candidate_status"] == "complete" and r.json()["bootable"] is True)

r = client.post("/devices/dev1/restart")
j = r.json()
token = j["boot_token"]
check("trial started on slot1", j["result"] == "trial" and j["running_slot"] == 1
      and isinstance(token, str) and len(token) >= 32 and j["pending_confirmation"] is True)
st = client.get("/devices/dev1").json()
check("state trial-pending, token visible",
      st["trial_state"] == "trial-pending" and st["pending_token"] == token
      and st["running_image"]["slot"] == 1
      and st["running_image"]["sha256"] == sha_v2)

print("== 6. 试启未决：两槽禁止写入 ==")
r = client.put("/devices/dev1/update", json={"length": 10, "sha256": "22" * 32})
check("declare locked 409", r.status_code == 409
      and r.json()["detail"]["code"] == "trial_pending_write_locked")
r = put_chunk(0, fw_v2[:10])
check("chunk locked 409", r.status_code == 409)

print("== 7. 错误令牌 409；正确确认 -> 替换已确认 ==")
r = client.post("/devices/dev1/confirm?token=wrong-token")
check("wrong token 409", r.status_code == 409
      and r.json()["detail"]["code"] == "invalid_token")
r = client.post("/devices/dev1/confirm", params={"token": token})
check("confirm 200", r.status_code == 200, r.status_code)
st = client.get("/devices/dev1").json()
check("confirmed/running slot1, no token",
      st["confirmed_slot"] == 1 and st["running_slot"] == 1
      and st["trial_state"] == "running-confirmed" and st["pending_token"] is None)
check("slot1 confirmed, slot0 superseded",
      st["slots"]["slot1"]["status"] == "confirmed"
      and st["slots"]["slot1"]["image_sha256"] == sha_v2
      and st["slots"]["slot0"]["status"] == "superseded"
      and st["slots"]["slot0"]["image_sha256"] == hashlib.sha256(fw_v1).hexdigest())
r = client.post("/devices/dev1/confirm", params={"token": token})
check("late confirm after success 409", r.status_code == 409)

print("== 8. 更新现在写入另一槽（槽0），试启后不确认再重启 -> 回退废弃 ==")
fw_v3 = b"FW3" + bytes(i % 256 for i in range(4997))
sha_v3 = hashlib.sha256(fw_v3).hexdigest()
client.put("/devices/dev1/update", json={"length": len(fw_v3), "sha256": sha_v3})
st = client.get("/devices/dev1").json()
check("update targets slot0", st["slots"]["slot0"]["status"] == "in-progress"
      and st["slots"]["slot1"]["status"] == "confirmed")
for off in range(0, len(fw_v3), 4096):
    client.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(fw_v3[off:off+4096])})
r = client.post("/devices/dev1/restart")
j = r.json()
token3 = j["boot_token"]
check("trial slot0 with token3", j["result"] == "trial" and j["running_slot"] == 0 and token3)

# 未确认再次重启 -> 回退
r = client.post("/devices/dev1/restart")
j = r.json()
check("rollback to slot1", j["result"] == "confirmed" and j["running_slot"] == 1
      and j["boot_token"] is None)
st = client.get("/devices/dev1").json()
check("rollback state clean", st["trial_state"] == "running-confirmed"
      and st["confirmed_slot"] == 1 and st["pending_token"] is None
      and st["slots"]["slot0"]["status"] == "empty")
r = client.post("/devices/dev1/confirm", params={"token": token3})
check("late confirm after rollback 409", r.status_code == 409
      and r.json()["detail"]["code"] == "no_trial_pending")

print("== 9. 服务重启（重新导入模块/重开连接）后状态保留 ==")
# 模拟分块中途服务重启：声明并传一半
fw_v4 = b"FW4" + bytes(i % 256 for i in range(19997))
sha_v4 = hashlib.sha256(fw_v4).hexdigest()
client.put("/devices/dev1/update", json={"length": len(fw_v4), "sha256": sha_v4})
client.put("/devices/dev1/chunks", json={"offset": 0, "data_base64": b64(fw_v4[:8000])})
# 进程重启：丢弃全部连接/模块级状态后重新建 client（DB 文件仍在）
importlib.reload(m)
client2 = TestClient(m.app)  # lifespan 会重新 init_db（CREATE IF NOT EXISTS）
with client2:
    pass
st = client2.get("/devices/dev1").json()
check("chunks survive service restart",
      st["slots"]["slot0"]["status"] == "in-progress"
      and st["slots"]["slot0"]["bytes_received"] == 8000)
# 接着传完并试启
for off in range(8000, len(fw_v4), 4096):
    client2.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(fw_v4[off:off+4096])})
r = client2.post("/devices/dev1/restart")
token4 = r.json()["boot_token"]
check("trial after resumed upload", r.json()["result"] == "trial" and token4)
# 再“重启服务”，trial-pending 与令牌必须还在
importlib.reload(m)
client3 = TestClient(m.app)
st = client3.get("/devices/dev1").json()
check("pending trial survives service restart",
      st["trial_state"] == "trial-pending" and st["pending_token"] == token4
      and st["running_slot"] == 0)
# 服务重启后仍禁止写入
r = client3.put("/devices/dev1/update", json={"length": 1, "sha256": "33" * 32})
check("still write-locked after service restart", r.status_code == 409)
r = client3.post("/devices/dev1/confirm", params={"token": token4})
check("confirm works after service restart", r.status_code == 200)
st = client3.get("/devices/dev1").json()
check("now confirmed slot0 fw_v4", st["confirmed_slot"] == 0
      and st["running_image"]["sha256"] == sha_v4)

print("== 10. 并发：多线程同时写声明/分块/重启，不产生损坏状态 ==")
client3.put("/devices/dev1/update", json={"length": 300, "sha256": "aa" * 32})
errors = []
def worker(n):
    try:
        c = TestClient(m.app)
        r = c.put("/devices/dev1/chunks", json={"offset": 100 * n, "data_base64": b64(b"c" * 100)})
        if r.status_code not in (200, 409):
            errors.append((n, r.status_code, r.text))
    except Exception as e:  # 任何序列化失败都不可接受
        errors.append((n, "exc", repr(e)))

ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
check("concurrent chunks no 5xx/exception", not errors, errors)
st = client3.get("/devices/dev1").json()
check("300 bytes assembled, digest wrong -> mismatch",
      st["slots"]["slot1"]["bytes_received"] == 300
      and st["slots"]["slot1"]["status"] == "digest-mismatch")

# 并发重启/确认竞争：只能有一个结果方向，且库状态一致
client3.put("/devices/dev1/update", json={"length": 300, "sha256": hashlib.sha256(b"c"*300).hexdigest()})
for i in range(3):
    client3.put("/devices/dev1/chunks", json={"offset": 100*i, "data_base64": b64(b"c"*100)})
st = client3.get("/devices/dev1").json()
check("candidate complete", st["slots"]["slot1"]["status"] == "complete")
tok = {"v": None}
def restart_once():
    c = TestClient(m.app)
    r = c.post("/devices/dev1/restart")
    if r.json().get("boot_token"):
        tok["v"] = r.json()["boot_token"]
def confirm_once():
    # 等令牌出现后确认；restart 与 confirm 并发只可能产生合法终态
    for _ in range(100):
        if tok["v"]:
            break
    if tok["v"]:
        c = TestClient(m.app)
        c.post("/devices/dev1/confirm", params={"token": tok["v"]})
t1 = threading.Thread(target=restart_once)
t2 = threading.Thread(target=confirm_once)
t1.start(); t2.start(); t1.join(); t2.join()
st = client3.get("/devices/dev1").json()
check("post-race state legal",
      st["trial_state"] in ("running-confirmed", "trial-pending"))
if st["trial_state"] == "trial-pending":
    # 确认没赶上：补确认收尾
    r = client3.post("/devices/dev1/confirm", params={"token": st["pending_token"]})
    check("cleanup confirm", r.status_code == 200)

print("== 11. 列表/候选查询/健康 ==")
check("health", client3.get("/health").json()["status"] == "ok")
check("device list", set(client3.get("/devices").json()["devices"]) >= {"dev1", "dev2"})
cand = client3.get("/devices/dev1/candidate").json()
final_st = client3.get("/devices/dev1").json()
expected_candidate = 1 - final_st["confirmed_slot"]
check("candidate view",
      cand["candidate_slot"] == expected_candidate
      and "chunks" in cand and cand["bootable"] is False, cand)

print("== 12. 受控交错：响应快照即本次操作完成时的状态，不混入后续写入 ==")

class CommitGate:
    """一次性提交闸门：包装 m._connect，让首个 commit 在落库成功后、
    响应返回前暂停，直到 may_return 置位。借此在主线程确定性制造
    「T1 已提交、响应未返回，T2 抢先写入」的受控交错。"""

    def __init__(self):
        self.committed = threading.Event()
        self.may_return = threading.Event()
        self._armed = True
        self._real_connect = m._connect

    def __enter__(self):
        gate = self
        real_connect = self._real_connect

        class HookedConn:
            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                self._inner.commit()
                if gate._armed:
                    gate._armed = False
                    gate.committed.set()
                    gate.may_return.wait(timeout=15)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        m._connect = lambda: HookedConn(real_connect())
        return self

    def __exit__(self, *exc):
        m._connect = self._real_connect
        self.may_return.set()  # 异常路径也放行，避免线程挂死
        return False


def run_gated(primary, compete):
    """线程中执行 primary；其首个 commit 成功后、响应返回前，
    由当前线程执行 compete()，然后放行 primary 并等其结束。"""
    with CommitGate() as gate:
        t = threading.Thread(target=primary)
        t.start()
        try:
            assert gate.committed.wait(timeout=15), "primary 未及时提交"
            compete()
        finally:
            gate.may_return.set()
            t.join(timeout=15)
    assert not t.is_alive(), "primary 线程未结束"


# 12a. PUT /update：T1 声明长度 3 提交后、返回查询前，T2 写入长度 6
client3.post("/devices", json={"device_id": "dev-race", "initial_image_base64": b64(b"base-image")})
resp1, resp2 = {}, {}

def declare_len3():
    c = TestClient(m.app)
    r = c.put("/devices/dev-race/update", json={"length": 3, "sha256": "44" * 32})
    resp1["status"], resp1["body"] = r.status_code, r.json()

def declare_len6():
    r = client3.put("/devices/dev-race/update", json={"length": 6, "sha256": "55" * 32})
    resp2["status"], resp2["body"] = r.status_code, r.json()

run_gated(declare_len3, declare_len6)
check("declare#1 (len 3) response reflects its own commit",
      resp1["status"] == 200
      and resp1["body"]["slots"]["slot1"]["declared_length"] == 3, resp1)
check("declare#2 (len 6) response reflects its own commit",
      resp2["status"] == 200
      and resp2["body"]["slots"]["slot1"]["declared_length"] == 6, resp2)
st = client3.get("/devices/dev-race").json()
check("final db = last declare (6), serial order preserved",
      st["slots"]["slot1"]["declared_length"] == 6
      and st["slots"]["slot1"]["declared_sha256"] == "55" * 32, st["slots"]["slot1"])

# 12b. POST /devices：创建提交后、返回前另有声明写入，创建响应保持初始状态
resp_create, resp_decl = {}, {}

def create_dev():
    c = TestClient(m.app)
    r = c.post("/devices", json={"device_id": "dev-race2",
                                 "initial_image_base64": b64(b"img-v1")})
    resp_create["status"], resp_create["body"] = r.status_code, r.json()

def declare_on_new():
    r = client3.put("/devices/dev-race2/update", json={"length": 9, "sha256": "66" * 32})
    resp_decl["status"], resp_decl["body"] = r.status_code, r.json()

run_gated(create_dev, declare_on_new)
check("create 201", resp_create["status"] == 201, resp_create.get("status"))
check("create response keeps initial state (slot1 empty, no declaration)",
      resp_create["body"]["slots"]["slot1"]["status"] == "empty"
      and resp_create["body"]["slots"]["slot1"]["declared_length"] is None,
      resp_create["body"]["slots"]["slot1"])
check("competing declare applied", resp_decl["status"] == 200
      and resp_decl["body"]["slots"]["slot1"]["declared_length"] == 9, resp_decl)
st = client3.get("/devices/dev-race2").json()
check("final db has the later declaration",
      st["slots"]["slot1"]["declared_length"] == 9
      and st["slots"]["slot1"]["status"] == "in-progress", st["slots"]["slot1"])

# 12c. POST /confirm：确认提交后、返回前另有声明写入，确认响应不带入该声明
client3.post("/devices", json={"device_id": "dev-race3", "initial_image_base64": b64(b"base3")})
fw_rc = b"C" * 100
sha_rc = hashlib.sha256(fw_rc).hexdigest()
client3.put("/devices/dev-race3/update", json={"length": 100, "sha256": sha_rc})
client3.put("/devices/dev-race3/chunks", json={"offset": 0, "data_base64": b64(fw_rc)})
tok_rc = client3.post("/devices/dev-race3/restart").json()["boot_token"]
resp_conf, resp_decl3 = {}, {}

def do_confirm():
    c = TestClient(m.app)
    r = c.post("/devices/dev-race3/confirm", params={"token": tok_rc})
    resp_conf["status"], resp_conf["body"] = r.status_code, r.json()

def declare_after_confirm():
    r = client3.put("/devices/dev-race3/update", json={"length": 7, "sha256": "77" * 32})
    resp_decl3["status"], resp_decl3["body"] = r.status_code, r.json()

run_gated(do_confirm, declare_after_confirm)
check("confirm 200", resp_conf["status"] == 200, resp_conf.get("status"))
check("confirm response: confirmed slot1, no later declaration",
      resp_conf["body"]["confirmed_slot"] == 1
      and resp_conf["body"]["trial_state"] == "running-confirmed"
      and resp_conf["body"]["slots"]["slot0"]["status"] == "superseded"
      and resp_conf["body"]["slots"]["slot0"]["declared_length"] is None,
      resp_conf["body"])
check("competing declare applied on slot0", resp_decl3["status"] == 200
      and resp_decl3["body"]["slots"]["slot0"]["declared_length"] == 7, resp_decl3)
st = client3.get("/devices/dev-race3").json()
check("final db: confirmed slot1 + declaration on slot0",
      st["confirmed_slot"] == 1
      and st["slots"]["slot0"]["declared_length"] == 7
      and st["slots"]["slot0"]["status"] == "in-progress", st["slots"]["slot0"])

print("== 13. 提交失败不得报成功，且不留部分写入 ==")
real_connect = m._connect

class FailingCommitConn:
    """commit 时抛错并回滚，模拟持久化失败。"""

    def __init__(self, inner):
        self._inner = inner

    def commit(self):
        self._inner.rollback()
        raise sqlite3.OperationalError("simulated commit failure")

    def __getattr__(self, name):
        return getattr(self._inner, name)

m._connect = lambda: FailingCommitConn(real_connect())
try:
    strict = TestClient(m.app, raise_server_exceptions=False)
    r = strict.put("/devices/dev-race/update", json={"length": 8, "sha256": "88" * 32})
    check("commit failure -> 5xx, never a success", r.status_code >= 500, r.status_code)
finally:
    m._connect = real_connect
st = client3.get("/devices/dev-race").json()
check("failed commit left no trace (still len 6)",
      st["slots"]["slot1"]["declared_length"] == 6
      and st["slots"]["slot1"]["declared_sha256"] == "55" * 32, st["slots"]["slot1"])

print(f"\nALL {PASS} CHECKS PASSED")
