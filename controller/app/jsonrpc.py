"""Gedeelde JSON-RPC-client voor FortiManager en FortiAnalyzer (/jsonrpc, sessie-login)."""
from __future__ import annotations

import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TASK_DONE, TASK_ERROR, TASK_ABORT = 4, 5, 7


class RpcError(RuntimeError):
    def __init__(self, url, code, msg):
        super().__init__(f"{url}: [{code}] {msg}")
        self.url, self.code, self.msg = url, code, msg


class JsonRpc:
    def __init__(self, ip: str, user: str, password: str, timeout=60):
        self.base = f"https://{ip}/jsonrpc"
        self.user, self.password, self.timeout = user, password, timeout
        self.session = None
        self._id = 0
        self.http = requests.Session()
        self.http.verify = False

    # --- transport --------------------------------------------------------------
    def _post(self, body: dict) -> dict:
        self._id += 1
        body = {**body, "id": self._id}
        if self.session:
            body["session"] = self.session
        r = self.http.post(self.base, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def login(self):
        res = self._post({"method": "exec", "params": [{"url": "/sys/login/user",
                                                         "data": {"user": self.user, "passwd": self.password}}]})
        st = res["result"][0]["status"]
        if st["code"] != 0:
            raise RpcError("/sys/login/user", st["code"], st["message"])
        self.session = res.get("session")
        return self

    def logout(self):
        try:
            self._post({"method": "exec", "params": [{"url": "/sys/logout"}]})
        except Exception:  # noqa: BLE001
            pass
        self.session = None

    def call(self, method: str, url: str, data=None, ok=(0,), **extra):
        """Eén JSON-RPC-call; geeft 'data' terug en gooit RpcError bij een foutcode die niet in 'ok' staat."""
        if not self.session:
            self.login()
        p = {"url": url, **extra}
        if data is not None:
            p["data"] = data
        res = self._post({"method": method, "params": [p]})["result"][0]
        st = res.get("status", {})
        if st.get("code", 0) not in ok:
            raise RpcError(url, st.get("code"), st.get("message"))
        return res.get("data")

    def get(self, url, **kw):    return self.call("get", url, **kw)
    def add(self, url, data, **kw):    return self.call("add", url, data, **kw)
    def set(self, url, data, **kw):    return self.call("set", url, data, **kw)
    def update(self, url, data, **kw): return self.call("update", url, data, **kw)
    def delete(self, url, **kw):       return self.call("delete", url, **kw)
    def exec(self, url, data=None, **kw): return self.call("exec", url, data, **kw)

    def upsert(self, url, data, **kw):
        """add; bestaat het object al (-2 duplicate), dan set."""
        try:
            return self.add(url, data, **kw)
        except RpcError as e:
            if e.code in (-2, -3, -10131):
                return self.set(url, data, **kw)
            raise

    def wait_task(self, task_id, log=None, timeout=900) -> dict:
        deadline, last = time.time() + timeout, -1
        changed = beat = time.time()
        while time.time() < deadline:
            t = self.get(f"/task/task/{task_id}") or {}
            if t.get("percent", 0) != last:
                last, changed = t.get("percent", 0), time.time()
                if log:
                    log(f"  taak {task_id}: {last}% {t.get('title', '')}")
            elif log and time.time() - beat > 30:      # hartslag: houdt de joblog levend en laat 'Stop' werken
                beat = time.time()
                log(f"  taak {task_id}: wacht ({last}%, {int(time.time() - changed)}s zonder voortgang)")
            if time.time() - changed > 300 and t.get("state") not in (TASK_DONE, 8):
                raise RpcError(f"/task/task/{task_id}", -2, "taak zonder voortgang (5 min): " + self.task_lines(task_id))
            if t.get("state") in (TASK_DONE, 8) or (t.get("percent") == 100 and t.get("state") == TASK_DONE):
                if t.get("num_err"):
                    raise RpcError(f"/task/task/{task_id}", t.get("num_err"), self.task_lines(task_id))
                return t
            if t.get("state") in (TASK_ERROR, TASK_ABORT):
                raise RpcError(f"/task/task/{task_id}", t.get("state"), self.task_lines(task_id))
            time.sleep(4)
        raise RpcError(f"/task/task/{task_id}", -1, "timeout")

    def task_lines(self, task_id) -> str:
        try:
            lines = self.get(f"/task/task/{task_id}/line") or []
            return "; ".join(f"{l.get('name', '')}: {l.get('detail', '')}" for l in lines)[:600]
        except Exception:  # noqa: BLE001
            return ""
