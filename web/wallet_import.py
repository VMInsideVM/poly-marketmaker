"""web/wallet_import.py — 批量导入钱包:粘贴文本解析(纯函数) + 后台导入任务状态。

格式:一行一个钱包 `私钥,代理,备注`,后两个字段可省,空行忽略。分隔符只能是逗号,
代理串本身就是 `host:port:账户:密码`,用冒号会拆不开。

导入本体(探代理协议 / 探账户类型 / 加密入库)在 web/routes.py 的 _import_wallet,
批量和单个导入走同一条路;这里只管解析和进度状态。
"""

import logging
import threading

logger = logging.getLogger(__name__)

MAX_FIELDS = 3


def parse_import_lines(text: str) -> list[dict]:
    """把粘贴的多行文本解析成待导入条目。

    返回 [{line_no, private_key, proxy, remark, error}, ...]。行号按原文本计,跳过
    空行不影响其余行的行号(用户要对着文本框数行)。格式不合法的行照样返回、带 error,
    一行坏掉不能让整批拒绝——私钥不能回显给用户核对,只能靠行号定位。

    私钥本身的合法性不在这里判,交给导入时的 _clean_private_key,避免两处校验跑偏。
    """
    rows = []
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.replace("，", ",").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) > MAX_FIELDS:
            rows.append(
                _row(
                    i, error=f"格式错误：一行最多 {MAX_FIELDS} 个字段（私钥,代理,备注）"
                )
            )
            continue
        parts += [""] * (MAX_FIELDS - len(parts))
        if not parts[0]:
            rows.append(_row(i, error="格式错误：私钥为空"))
            continue
        rows.append(_row(i, private_key=parts[0], proxy=parts[1], remark=parts[2]))
    return rows


def _row(line_no, private_key="", proxy="", remark="", error=""):
    return {
        "line_no": line_no,
        "private_key": private_key,
        "proxy": proxy,
        "remark": remark,
        "error": error,
    }


class ImportJob:
    """一次批量导入的进度与逐行结果(单用户单进程,同一时刻只允许一个任务在跑)。

    结果里绝不带私钥:页面上只回显行号、备注和导出的地址。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.total = 0
        self.done = 0
        self.results: list[dict] = []

    def start(self, total: int) -> bool:
        """占位开跑。已有任务在跑时返回 False,调用方据此拒绝重复提交。"""
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.total = total
            self.done = 0
            self.results = []
            return True

    def add(self, result: dict):
        with self.lock:
            self.results.append(result)
            self.done += 1

    def finish(self):
        with self.lock:
            self.running = False

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "total": self.total,
                "done": self.done,
                "results": list(self.results),
            }
