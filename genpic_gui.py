#!/usr/bin/env python3
"""
genpic_gui —— Runpod Serverless 生图 GUI 请求器（Anima / Qwen-Image-2.1）

设计重点（按需求）：**把可能的报错日志完整留档**，方便你自己反复试。

落盘位置（都在本脚本同级的 logs\\ 下）：
    logs\\gui_<日期>.log              时间线：每次操作一行
    logs\\req\\NNN_<endpoint>_request.json     每个请求的完整 payload（原样）
    logs\\res\\NNN_<endpoint>_response.json    每个响应的完整 body（原样）
    logs\\fail\\<时间>_<endpoint>_<job>.txt    ★ 失败现场报告，见下

失败现场报告里装了什么（一次抓全，不用再问人）：
    - 请求 URL / HTTP 状态码 / 耗时 / 完整请求体
    - job 的状态、executionTime、delayTime、error 全文、output.errors
    - endpoint 的 workerHealth
    - 该 endpoint 所有 worker 的实时状态（id / status / GPU / 机房 / 启动时间）
    - 每个 worker 的 container + system 日志（SSE，抓 N 行）★ 状态会骗人，日志才作数
    - Python traceback（如果是客户端异常）

为什么日志这么重要：worker 状态 `IDLE` / `unhealthy=0` / 日志里 `worker is ready`
都可能与「ComfyUI 其实已经死了」同时成立，只有 worker 日志能揭穿（2026-10-16 实测踩过）。

用法
----
    python genpic_gui.py

API key 优先读环境变量 RUNPOD_API_KEY，也可以直接在界面上填（勾「记住」才会写进
genpic_gui_config.json —— 明文，自己权衡）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import genpic  # noqa: E402  复用已验证的 payload 构造逻辑（单一实现）

LOGS = HERE / "logs"
LOG_REQ = LOGS / "req"
LOG_RES = LOGS / "res"
LOG_FAIL = LOGS / "fail"
OUTPUTS = HERE / "outputs"
CONFIG = HERE / "genpic_gui_config.json"

ENDPOINT_API = "https://api.runpod.ai/v2"        # 提交 / 查询 / 取消（endpoint API）
MANAGE_API = "https://api.runpod.io/v2"          # worker 列表 / worker 日志（管理 API）

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}

# 默认值跟 genpic.py 的 argparse 保持一致
DEFAULTS = {
    "anima": dict(width=1024, height=1024, steps=30, cfg=4.0, denoise=0.6,
                  turbo_steps=8, turbo_cfg=1.0,
                  model="anima-base-v1.0.safetensors", turbo_lora="anima-turbo-lora-v0.2.safetensors"),
    "qwen21": dict(width=1024, height=1024, steps=25, cfg=1.0, resolution=1024,
                   model="qwen_image_2.1_int8_convrot.safetensors"),
}

# 宽/高：ComfyUI 要求 8 的倍数，这里按 64 步长规整。
# 下限 256 是「能看出东西」的最小尺寸；上限 2048 = Qwen-Image-2.1 的原生 2K，
# 再往上在 16/24GB 卡上就是显存自杀（权重本身已经吃掉 16GB）。
DIM_MIN, DIM_MAX, DIM_STEP = 256, 2048, 64

PRESETS = {
    "anima": {
        "endpoint_id": "2r1gunfjxjo1jh",
        "hint": "Anima 2B · AMPERE_16 · $0.58/hr · 文生图 + 单图 img2img",
        "prompt": "1girl, solo, long black hair, glowing eyes, sharp domineering cool style",
        "negative": "",
    },
    "qwen21": {
        "endpoint_id": "sfhy5bkcvvsojm",
        "hint": "Qwen-Image-2.1 7B · AMPERE_24 · $0.69/hr · 文生图 + 1~10 张参考图编辑",
        "prompt": "a ceramic fox figurine on a blue table, soft window light",
        "negative": "",
    },
}


# =========================================================================== 日志

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dirs() -> None:
    for d in (LOGS, LOG_REQ, LOG_RES, LOG_FAIL, OUTPUTS):
        d.mkdir(parents=True, exist_ok=True)


class Recorder:
    """所有落盘动作集中在这里，方便一处改、处处生效。"""

    def __init__(self) -> None:
        ensure_dirs()
        self.seq = 0
        self.timeline = LOGS / f"gui_{datetime.now():%Y%m%d}.log"

    def event(self, text: str) -> None:
        try:
            with self.timeline.open("a", encoding="utf-8") as f:
                f.write(f"{ts_full()}  {text}\n")
        except OSError:
            pass

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def save_request(self, endpoint: str, url: str, payload) -> Path:
        n = self._next()
        p = LOG_REQ / f"{n:03d}_{endpoint}_request.json"
        p.write_text(json.dumps({"url": url, "payload": payload}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        self.event(f"REQ #{n} -> {url}  (payload {len(json.dumps(payload))}B)  {p.name}")
        return p

    def save_response(self, endpoint: str, url: str, status: int, body_text: str) -> Path:
        n = self._next()
        p = LOG_RES / f"{n:03d}_{endpoint}_response.json"
        p.write_text(json.dumps({"url": url, "httpStatus": status, "body": body_text},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        self.event(f"RES #{n} <- HTTP {status}  ({len(body_text)}B)  {p.name}")
        return p

    def save_failure(self, endpoint: str, job_id: str, text: str) -> Path:
        p = LOG_FAIL / f"{stamp()}_{endpoint}_{job_id or 'nojob'}.txt"
        p.write_text(text, encoding="utf-8")
        self.event(f"FAILURE REPORT -> {p}")
        return p


def build_failure_report(title: str, sections: list[tuple[str, str]]) -> str:
    head = [
        "=" * 78,
        f"  {title}",
        f"  {ts_full()}  (local)",
        "=" * 78,
        "",
    ]
    body = []
    for name, content in sections:
        body.append(f"---------- {name} ----------")
        body.append(content.rstrip("\n") if content else "(空)")
        body.append("")
    return "\n".join(head + body)


# =========================================================================== 网络

class Api:
    """一个 endpoint API + 管理 API 的薄封装。每个调用都落盘。"""

    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    # ---- 底层 ----
    def _call(self, method: str, url: str, key: str, payload=None, timeout: int = 60):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return resp.status, text, time.time() - t0
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            return e.code, text, time.time() - t0
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误（连不上 {url}）：{e}") from e
        except socket.timeout as e:
            raise RuntimeError(f"请求超时（{timeout}s，{url}）") from e

    # ---- endpoint API ----
    def submit(self, endpoint: str, key: str, payload: dict, sync: bool, timeout: int):
        op = "runsync" if sync else "run"
        url = f"{ENDPOINT_API}/{endpoint}/{op}"
        self.rec.save_request(endpoint, url, payload)
        status, text, elapsed = self._call("POST", url, key, payload, timeout)
        self.rec.save_response(endpoint, url, status, text)
        body = self._parse(endpoint, url, status, text)
        return body, text, status, elapsed, url

    def job_status(self, endpoint: str, key: str, job_id: str, timeout: int = 60):
        url = f"{ENDPOINT_API}/{endpoint}/status/{job_id}"
        status, text, elapsed = self._call("GET", url, key, None, timeout)
        self.rec.save_response(endpoint, url, status, text)
        return self._parse(endpoint, url, status, text), text, status, elapsed, url

    def cancel(self, endpoint: str, key: str, job_id: str):
        url = f"{ENDPOINT_API}/{endpoint}/cancel/{job_id}"
        status, text, elapsed = self._call("POST", url, key, None, 60)
        self.rec.save_response(endpoint, url, status, text)
        return text, status

    # ---- 管理 API ----
    def workers(self, endpoint: str, key: str):
        url = f"{MANAGE_API}/serverless/{endpoint}/workers"
        status, text, _ = self._call("GET", url, key, None, 60)
        if status >= 400:
            return {"error": f"HTTP {status}: {text[:400]}"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": f"非 JSON 响应: {text[:400]}"}

    def worker_logs(self, endpoint: str, key: str, worker_id: str,
                    source: str = "both", tail: int = 400, max_seconds: int = 25):
        """worker 日志是 SSE 流，这里读到 max_seconds 或断流为止。"""
        url = (f"{MANAGE_API}/serverless/{endpoint}/workers/{worker_id}/logs"
               f"?source={source}&tail={tail}")
        req = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "text/event-stream"},
        )
        lines: list[str] = []
        deadline = time.time() + max_seconds
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                for raw in resp:
                    if time.time() > deadline:
                        lines.append(f"[... 到 {max_seconds}s 上限，截断]")
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        try:
                            j = json.loads(payload)
                            lines.append(f"{j.get('ts','')} [{j.get('source','?')}] {j.get('line','')}")
                        except json.JSONDecodeError:
                            lines.append(payload)
        except Exception as e:  # noqa: BLE001  日志抓取失败不能影响主流程
            lines.append(f"[抓日志失败] {type(e).__name__}: {e}")
        if not lines:
            lines.append("(没有日志：worker 可能已经被回收，或这段区间确实无输出)")
        return url, lines

    @staticmethod
    def _parse(endpoint: str, url: str, status: int, text: str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"服务端返回的不是 JSON（HTTP {status}）：{text[:600]}") from None


# =========================================================================== GUI

class DimControl:
    """宽/高控件：数字输入框 + 滑块，双向同步。

    值一律规整到 DIM_STEP 的倍数并夹在 [DIM_MIN, DIM_MAX]：
    - 拖滑块 -> 输入框立刻跟着变（规整后的整数）
    - 输入框里打字 -> 只有落在范围内才推给滑块；按回车 / 失焦时规整并回写
    """

    def __init__(self, parent, label: str, var: tk.StringVar) -> None:
        self.var = var
        self.guard = False
        self.int = tk.IntVar(value=self._norm(var.get()))
        self.frame = ttk.Frame(parent)
        ttk.Label(self.frame, text=label, width=3).pack(side="left")
        self.entry = ttk.Entry(self.frame, textvariable=var, width=6)
        self.entry.pack(side="left", padx=(0, 6))
        self.scale = ttk.Scale(self.frame, from_=DIM_MIN, to=DIM_MAX, orient="horizontal",
                               variable=self.int, length=190, command=self._from_scale)
        self.scale.pack(side="left")
        var.trace_add("write", self._from_entry)
        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<FocusOut>", self._commit)

    @staticmethod
    def _norm(raw) -> int:
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            n = 1024
        n = max(DIM_MIN, min(DIM_MAX, n))
        return int(round(n / DIM_STEP) * DIM_STEP)

    def _set_var(self, n: int) -> None:
        self.guard = True
        self.var.set(str(n))
        self.guard = False

    def _from_scale(self, _=None) -> None:
        if not self.guard:
            self._set_var(self._norm(self.int.get()))

    def _from_entry(self, *_args) -> None:
        if self.guard:
            return
        try:
            n = int(self.var.get().strip())
        except ValueError:
            return
        if DIM_MIN <= n <= DIM_MAX:
            self.int.set(n)

    def _commit(self, _=None) -> None:
        n = self._norm(self.var.get())
        self._set_var(n)
        self.int.set(n)

    def value(self) -> int:
        self._commit()
        return int(self.var.get())


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        self.rec = Recorder()
        self.api = Api(self.rec)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.current_job: str | None = None
        self.stop_poll = threading.Event()
        self.last_report: str = ""
        self.preview_img = None

        self.title("genpic GUI —— Runpod 生图请求器")
        self.geometry("1280x860")

        self._build_vars()
        self._build_ui()
        self._load_config()
        self.after(120, self._drain)
        self.log("就绪。日志目录：" + str(LOGS))
        self.log("提示：key 优先取环境变量 RUNPOD_API_KEY，界面里填的只在勾了「记住」时才落盘。")

    # ------------------------------------------------------------ 变量
    def _build_vars(self) -> None:
        cfg = {}
        if CONFIG.exists():
            try:
                cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}

        self.engine = tk.StringVar(value=cfg.get("engine", "anima"))
        self.endpoint = tk.StringVar()
        self.key = tk.StringVar(value=os.environ.get("RUNPOD_API_KEY", "") or cfg.get("key", ""))
        self.remember_key = tk.BooleanVar(value=bool(cfg.get("remember_key")))
        self.width = tk.StringVar(value="1024")
        self.height = tk.StringVar(value="1024")
        self.steps = tk.StringVar(value="")
        self.cfgv = tk.StringVar(value="")
        self.seed = tk.StringVar(value="")
        self.denoise = tk.StringVar(value="0.6")
        self.resolution = tk.StringVar(value="1024")
        self.model = tk.StringVar()
        self.turbo = tk.BooleanVar(value=False)
        self.sync = tk.BooleanVar(value=True)
        self.timeout = tk.StringVar(value="900")
        self.poll_interval = tk.StringVar(value="4")
        self.grab_logs = tk.BooleanVar(value=True)
        self.log_tail = tk.StringVar(value="400")
        self.images: list[str] = []
        self._apply_preset("anima", cfg)

    def _apply_preset(self, engine: str, cfg: dict | None = None) -> None:
        p = PRESETS[engine]
        d = DEFAULTS[engine]
        cfg = cfg or {}
        self.engine.set(engine)
        self.endpoint.set(cfg.get("endpoint_id", {}).get(engine, p["endpoint_id"]) if cfg else p["endpoint_id"])
        if hasattr(self, "prompt_text"):
            self._set_text(self.prompt_text, p["prompt"])
            self._set_text(self.negative_text, p["negative"])
        self.width.set(str(d["width"]))
        self.height.set(str(d["height"]))
        self.steps.set(str(d["steps"]))
        self.cfgv.set(str(d["cfg"]))
        self.seed.set("")
        self.denoise.set(str(d.get("denoise", 0.6)))
        self.resolution.set(str(d.get("resolution", 1024)))
        self.model.set(d["model"])
        self.turbo.set(False)
        self.images = []
        if hasattr(self, "img_list"):
            self._refresh_images()
            self._sync_widgets()

    def _sync_widgets(self) -> None:
        engine = self.engine.get()
        if hasattr(self, "btn_add_img"):
            self.btn_add_img.configure(state="normal")
        if hasattr(self, "hint"):
            if engine == "anima":
                self.hint.configure(text=PRESETS["anima"]["hint"] + "  ·  最多 1 张参考图")
            else:
                self.hint.configure(text=PRESETS["qwen21"]["hint"] + "  ·  可 1~10 张")

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="引擎").pack(side="left")
        for name, label in (("anima", "anima（动漫）"), ("qwen21", "qwen21（通用）")):
            ttk.Radiobutton(top, text=label, value=name, variable=self.engine,
                            command=self._on_engine).pack(side="left", padx=(6, 0))

        ttk.Label(top, text="   Endpoint ID").pack(side="left")
        ttk.Entry(top, textvariable=self.endpoint, width=22).pack(side="left", padx=4)
        ttk.Label(top, text="API key").pack(side="left")
        ttk.Entry(top, textvariable=self.key, width=34, show="*").pack(side="left", padx=4)
        ttk.Checkbutton(top, text="记住", variable=self.remember_key).pack(side="left")

        self.hint = ttk.Label(self, text="", foreground="#555")
        self.hint.pack(fill="x", padx=12, pady=(0, 6))

        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # ---- 左：参数
        pf = ttk.LabelFrame(left, text="参数", padding=8)
        pf.pack(fill="x")

        ttk.Label(pf, text="Prompt").grid(row=0, column=0, sticky="nw", pady=2)
        self.prompt_text = tk.Text(pf, height=4, width=52, wrap="word", font=("Consolas", 9))
        self.prompt_text.grid(row=0, column=1, sticky="we", pady=2)
        ttk.Label(pf, text="Negative").grid(row=1, column=0, sticky="nw", pady=2)
        self.negative_text = tk.Text(pf, height=2, width=52, wrap="word", font=("Consolas", 9))
        self.negative_text.grid(row=1, column=1, sticky="we", pady=2)

        ttk.Label(pf, text="尺寸", foreground="#555").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        dims = ttk.Frame(pf)
        dims.grid(row=2, column=1, sticky="we", pady=(6, 0))
        self.dim_w = DimControl(dims, "宽", self.width)
        self.dim_w.frame.pack(anchor="w")
        self.dim_h = DimControl(dims, "高", self.height)
        self.dim_h.frame.pack(anchor="w", pady=(2, 0))
        ttk.Label(dims, text=f"{DIM_MIN}~{DIM_MAX}，{DIM_STEP} 的倍数；1MP≈1024×1024，越大越慢越吃显存",
                  foreground="#777").pack(anchor="w", pady=(2, 0))

        dims2 = ttk.Frame(pf)
        dims2.grid(row=3, column=0, columnspan=2, sticky="we", pady=(6, 2))
        ttk.Label(dims2, text="steps").grid(row=0, column=0, sticky="e")
        ttk.Entry(dims2, textvariable=self.steps, width=6).grid(row=0, column=1, padx=(2, 8))
        ttk.Label(dims2, text="cfg").grid(row=0, column=2, sticky="e")
        ttk.Entry(dims2, textvariable=self.cfgv, width=6).grid(row=0, column=3, padx=(2, 8))
        ttk.Label(dims2, text="seed").grid(row=0, column=4, sticky="e")
        ttk.Entry(dims2, textvariable=self.seed, width=8).grid(row=0, column=5, padx=(2, 2))
        ttk.Button(dims2, text="随机", width=5, command=self._random_seed).grid(row=0, column=6, padx=(0, 8))

        dims3 = ttk.Frame(pf)
        dims3.grid(row=4, column=0, columnspan=2, sticky="we", pady=2)
        ttk.Label(dims3, text="denoise").grid(row=0, column=0, sticky="e")
        ttk.Entry(dims3, textvariable=self.denoise, width=6).grid(row=0, column=1, padx=(2, 8))
        ttk.Label(dims3, text="resolution").grid(row=0, column=2, sticky="e")
        ttk.Entry(dims3, textvariable=self.resolution, width=6).grid(row=0, column=3, padx=(2, 8))
        ttk.Label(dims3, text="(denoise 仅 img2img；resolution 是参考图预处理预算)",
                  foreground="#777").grid(row=0, column=4, sticky="w")

        ttk.Label(pf, text="model").grid(row=5, column=0, sticky="e", pady=2)
        ttk.Entry(pf, textvariable=self.model, width=52).grid(row=5, column=1, sticky="we", pady=2)
        ttk.Checkbutton(pf, text="turbo（anima：8 步 / CFG 1，需镜像内有 turbo LoRA）",
                        variable=self.turbo, command=self._on_turbo).grid(
            row=6, column=0, columnspan=2, sticky="w")
        pf.columnconfigure(1, weight=1)

        # ---- 左：参考图
        gf = ttk.LabelFrame(left, text="参考图（anima 最多 1 张；qwen21 1~10 张）", padding=8)
        gf.pack(fill="x", pady=(8, 0))
        self.img_list = tk.Listbox(gf, height=6, width=58)
        self.img_list.pack(fill="x")
        row = ttk.Frame(gf)
        row.pack(fill="x", pady=(4, 0))
        self.btn_add_img = ttk.Button(row, text="添加…", command=self._add_images)
        self.btn_add_img.pack(side="left")
        ttk.Button(row, text="移除选中", command=self._remove_image).pack(side="left", padx=4)
        ttk.Button(row, text="清空", command=self._clear_images).pack(side="left")
        self.img_info = ttk.Label(gf, text="0 张 → 走文生图", foreground="#555")
        self.img_info.pack(anchor="w", pady=(4, 0))

        # ---- 左：发送控制
        cf = ttk.LabelFrame(left, text="发送", padding=8)
        cf.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(cf, text="/runsync（阻塞等结果）", variable=self.sync).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(cf, text="超时(s)").grid(row=1, column=0, sticky="e")
        ttk.Entry(cf, textvariable=self.timeout, width=8).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(cf, text="轮询间隔(s)").grid(row=2, column=0, sticky="e")
        ttk.Entry(cf, textvariable=self.poll_interval, width=8).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(cf, text="失败时自动抓 worker 日志", variable=self.grab_logs).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(cf, text="日志行数").grid(row=4, column=0, sticky="e")
        ttk.Entry(cf, textvariable=self.log_tail, width=8).grid(row=4, column=1, sticky="w")

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(8, 0))
        self.btn_preview = ttk.Button(btns, text="预览请求体", command=self._preview)
        self.btn_preview.pack(fill="x", pady=2)
        self.btn_send = ttk.Button(btns, text="发送", command=self._send)
        self.btn_send.pack(fill="x", pady=2)
        self.btn_status = ttk.Button(btns, text="查状态", command=self._check_status, state="disabled")
        self.btn_status.pack(fill="x", pady=2)
        self.btn_cancel = ttk.Button(btns, text="取消任务", command=self._cancel, state="disabled")
        self.btn_cancel.pack(fill="x", pady=2)
        ttk.Button(btns, text="手动抓 worker 日志", command=self._grab_logs_now).pack(fill="x", pady=2)
        ttk.Button(btns, text="保存配置", command=self._save_config).pack(fill="x", pady=2)

        # ---- 右：结果 + 日志
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        res = ttk.Frame(nb, padding=8)
        nb.add(res, text="结果")
        self.status_line = ttk.Label(res, text="（还没发过请求）", font=("Consolas", 10))
        self.status_line.pack(anchor="w")
        self.preview_label = ttk.Label(res, text="生成的图片会显示在这里")
        self.preview_label.pack(anchor="w", pady=6)
        rrow = ttk.Frame(res)
        rrow.pack(anchor="w")
        ttk.Button(rrow, text="保存图片到 outputs\\", command=self._save_image).pack(side="left")
        self.raw_out = scrolledtext.ScrolledText(res, height=12, wrap="none", font=("Consolas", 9))
        self.raw_out.pack(fill="both", expand=True, pady=(6, 0))

        logf = ttk.Frame(nb, padding=8)
        nb.add(logf, text="日志 / 报错")
        self.log_text = scrolledtext.ScrolledText(logf, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("err", foreground="#b00020")
        self.log_text.tag_configure("ok", foreground="#0a7a0a")
        lrow = ttk.Frame(logf)
        lrow.pack(fill="x", pady=(4, 0))
        ttk.Button(lrow, text="复制最后一条报错", command=self._copy_last_error).pack(side="left")
        ttk.Button(lrow, text="清空", command=lambda: self.log_text.delete("1.0", "end")).pack(side="left", padx=4)
        ttk.Button(lrow, text="打开日志目录", command=self._open_logs).pack(side="left")

        self._sync_widgets()
        self._apply_preset(self.engine.get())      # 文本框建好之后再把预设内容填进去

    def _open_logs(self) -> None:
        try:
            os.startfile(LOGS)
        except OSError as e:
            self.log(f"打不开目录（{e}），路径是：{LOGS}", "err")

    # ------------------------------------------------------------ 小动作
    def _on_engine(self) -> None:
        self._apply_preset(self.engine.get())

    # ------------------------------------------------------------ 文本框/联动
    @staticmethod
    def _get_text(widget) -> str:
        return widget.get("1.0", "end").strip()

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", text)

    def _on_turbo(self) -> None:
        d = DEFAULTS["anima"]
        if self.engine.get() != "anima":
            if self.turbo.get():
                self.turbo.set(False)
                self.log("turbo 只对 anima 有意义（要用镜像里的 Anima turbo LoRA）", "err")
            return
        if self.turbo.get():
            self.steps.set(str(d["turbo_steps"]))
            self.cfgv.set(str(d["turbo_cfg"]))
            self.log(f"turbo 开 → steps {d['turbo_steps']} / CFG {d['turbo_cfg']}（采样步数变少，画风会变）")
        else:
            self.steps.set(str(d["steps"]))
            self.cfgv.set(str(d["cfg"]))
            self.log(f"turbo 关 → steps {d['steps']} / CFG {d['cfg']}")

    def _random_seed(self) -> None:
        import secrets
        self.seed.set(str(secrets.randbits(53)))

    def _add_images(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选参考图", filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("全部", "*.*")])
        if not paths:
            return
        for p in paths:
            if p not in self.images:
                self.images.append(p)
        if self.engine.get() == "anima" and len(self.images) > 1:
            self.log("anima 只支持单图 img2img，多余的已丢弃（保留第一张）", "err")
            self.images = self.images[:1]
        if self.engine.get() == "qwen21" and len(self.images) > 10:
            self.log("qwen21 最多 10 张参考图，多余的已丢弃", "err")
            self.images = self.images[:10]
        self._refresh_images()

    def _remove_image(self) -> None:
        for i in reversed(self.img_list.curselection()):
            del self.images[i]
        self._refresh_images()

    def _clear_images(self) -> None:
        self.images = []
        self._refresh_images()

    def _refresh_images(self) -> None:
        self.img_list.delete(0, "end")
        for p in self.images:
            self.img_list.insert("end", p)
        n = len(self.images)
        if self.engine.get() == "anima":
            self.img_info.configure(text=f"{n} 张 → " + ("图生图 img2img" if n else "文生图"))
        else:
            self.img_info.configure(text=f"{n} 张 → " + ("多图编辑" if n else "文生图"))

    # ------------------------------------------------------------ 日志面板
    def log(self, text: str, tag: str | None = None) -> None:
        for line in str(text).splitlines() or [""]:
            self.log_text.insert("end", f"[{ts()}] {line}\n", tag or ())
        self.log_text.see("end")

    def _copy_last_error(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.last_report or self.log_text.get("1.0", "end")[-4000:])
        messagebox.showinfo("已复制", "最后一条报错（或日志尾部）已复制到剪贴板。")

    # ------------------------------------------------------------ 配置
    def _load_config(self) -> None:
        if not CONFIG.exists():
            return
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        ep = (cfg.get("endpoint_id") or {}).get(self.engine.get())
        if ep:
            self.endpoint.set(ep)

    def _save_config(self) -> None:
        cfg = {
            "engine": self.engine.get(),
            "endpoint_id": {k: (self.endpoint.get() if k == self.engine.get() else v["endpoint_id"])
                            for k, v in PRESETS.items()},
            "remember_key": self.remember_key.get(),
        }
        if self.remember_key.get():
            cfg["key"] = self.key.get()
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"配置已写入 {CONFIG}" + ("（含明文 key）" if self.remember_key.get() else ""))

    # ------------------------------------------------------------ payload
    def _build_payload(self) -> dict:
        engine = self.engine.get()
        if not self.endpoint.get().strip():
            raise ValueError("Endpoint ID 是空的")
        prompt = self._get_text(self.prompt_text)
        if not prompt:
            raise ValueError("Prompt 是空的")

        def num(var, name, cast, default=None):
            """空 -> default（steps/cfg 这类给默认值）；填了就必须是合法数字。"""
            raw = var.get().strip()
            if not raw:
                return default
            try:
                return cast(raw)
            except ValueError:
                raise ValueError(f"{name} 不是合法数字：{raw!r}") from None

        d = DEFAULTS[engine]
        seed = num(self.seed, "seed", int)
        if seed is None:
            import secrets
            seed = secrets.randbits(53)
            self.seed.set(str(seed))

        width, height = self.dim_w.value(), self.dim_h.value()
        negative = self._get_text(self.negative_text)

        if engine == "anima":
            args = argparse.Namespace(
                which="anime",
                prompt=prompt,
                negative=negative,
                image=(self.images[0] if self.images else None),
                width=width,
                height=height,
                steps=num(self.steps, "steps", int, d["steps"]),
                cfg=num(self.cfgv, "cfg", float, d["cfg"]),
                denoise=num(self.denoise, "denoise", float, d["denoise"]),
                model=self.model.get().strip() or d["model"],
                turbo=self.turbo.get(),
                turbo_lora=d["turbo_lora"],
                seed=seed,
            )
            return genpic.build_anima(args)

        args = argparse.Namespace(
            which="qwen",
            prompt=prompt,
            negative=negative,
            image=list(self.images),
            width=width,
            height=height,
            resolution=num(self.resolution, "resolution", int, 1024) or 1024,
            steps=num(self.steps, "steps", int, d["steps"]),
            cfg=num(self.cfgv, "cfg", float, d["cfg"]),
            model=self.model.get().strip() or d["model"],
            seed=seed,
        )
        return genpic.build_qwen(args)

    def _payload_or_warn(self):
        try:
            payload = self._build_payload()
        except SystemExit as e:  # genpic 里用 sys.exit 报错
            self.log(f"构造请求失败：{e}", "err")
            messagebox.showerror("构造请求失败", str(e))
            return None
        except Exception as e:  # noqa: BLE001
            self.log(f"构造请求失败：{type(e).__name__}: {e}", "err")
            messagebox.showerror("构造请求失败", str(e))
            return None
        return payload

    # ------------------------------------------------------------ 按钮
    def _preview(self) -> None:
        payload = self._payload_or_warn()
        if payload is None:
            return
        engine = self.engine.get()
        n_img = len(payload["input"].get("images", []))
        n_nodes = len(payload["input"]["workflow"])
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        shown = text if len(text) < 4000 else text[:4000] + "\n…(截断，完整见 logs\\req\\)"
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", shown)
        self.log(f"预览：{engine} · workflow {n_nodes} 个节点 · 参考图 {n_img} 张 · payload {len(text)}B")

    def _send(self) -> None:
        if self.busy:
            messagebox.showwarning("正忙", "上一个请求还没结束")
            return
        if not self.key.get().strip():
            messagebox.showerror("缺 API key", "请填 API key，或设置环境变量 RUNPOD_API_KEY")
            return
        payload = self._payload_or_warn()
        if payload is None:
            return
        self._preview_from_payload(payload)

        sync = self.sync.get()
        try:
            timeout = int(self.timeout.get() or 900)
        except ValueError:
            timeout = 900

        self._set_busy(True)
        self.stop_poll.clear()

        def work():
            try:
                body, text, status, elapsed, url = self.api.submit(
                    self.endpoint.get().strip(), self.key.get().strip(), payload, sync, timeout)
                self.q.put(("submitted", dict(body=body, text=text, status=status,
                                              elapsed=elapsed, url=url, payload=payload, sync=sync)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("client_error", dict(exc=e, payload=payload,
                                                 where="发送请求时")))

        threading.Thread(target=work, daemon=True).start()

    def _preview_from_payload(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", text if len(text) < 4000 else text[:4000] + "\n…(截断)")

    def _check_status(self) -> None:
        if not self.current_job:
            return
        self._set_busy(True)

        def work():
            try:
                body, text, status, elapsed, url = self.api.job_status(
                    self.endpoint.get().strip(), self.key.get().strip(), self.current_job)
                self.q.put(("status", dict(body=body, text=text, status=status, elapsed=elapsed, url=url)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("client_error", dict(exc=e, payload=None, where="查询状态时")))

        threading.Thread(target=work, daemon=True).start()

    def _cancel(self) -> None:
        if not self.current_job:
            return
        self.stop_poll.set()
        ep, key, job = self.endpoint.get().strip(), self.key.get().strip(), self.current_job

        def work():
            try:
                text, status = self.api.cancel(ep, key, job)
                self.q.put(("log", f"取消请求已发出：HTTP {status} {text[:200]}"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"取消失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _grab_logs_now(self) -> None:
        ep, key = self.endpoint.get().strip(), self.key.get().strip()
        if not ep or not key:
            messagebox.showerror("缺参数", "需要 Endpoint ID 和 API key")
            return
        self._set_busy(True)

        def work():
            try:
                sections = self._collect_worker_evidence(ep, key, prefix="手动抓取")
                self.q.put(("evidence", sections))
            except Exception as e:  # noqa: BLE001
                self.q.put(("client_error", dict(exc=e, payload=None, where="抓 worker 日志时")))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 证据收集
    def _collect_worker_evidence(self, ep: str, key: str, prefix: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        w = self.api.workers(ep, key)
        ws = (w or {}).get("workers") or []
        summary = (w or {}).get("summary") or {}
        lines = [f"summary: {json.dumps(summary, ensure_ascii=False)}"]
        for x in ws:
            lines.append(f"  - {x.get('id')}  {x.get('status')}  {x.get('gpuTypeId')}  "
                         f"{x.get('dataCenterId')}  startedAt={x.get('startedAt')}  "
                         f"uptime={x.get('uptimeSeconds')}")
        if (w or {}).get("error"):
            lines.append(f"  (接口报错) {w['error']}")
        if not ws:
            lines.append("  (没有 worker：可能已回收；job 的 error 就是全部信息了)")
        sections.append((f"{prefix} · 该 endpoint 的 worker 列表 (/v2/serverless/<id>/workers)",
                         "\n".join(lines)))

        try:
            tail = int(self.log_tail.get() or 400)
        except ValueError:
            tail = 400
        for x in ws:
            wid = x.get("id")
            if not wid:
                continue
            url, log_lines = self.api.worker_logs(ep, key, wid, tail=tail)
            sections.append((f"{prefix} · worker {wid} 日志（{url}）", "\n".join(log_lines)))
        return sections

    # ------------------------------------------------------------ 队列循环
    def _drain(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "submitted":
                    self._on_submitted(data)
                elif kind == "status":
                    self._on_status(data)
                elif kind == "evidence":
                    self._set_busy(False)
                    for name, content in data:
                        self.log(f"---- {name} ----")
                        self.log(content)
                elif kind == "client_error":
                    self._on_client_error(data)
                elif kind == "ok":
                    self._set_busy(False)
                elif kind == "log":
                    self.log(str(data))
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _on_client_error(self, data: dict) -> None:
        self._set_busy(False)
        exc = data.get("exc")
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            tb = "(无 traceback)"
        report = build_failure_report(
            f"客户端异常 —— {data.get('where','')}",
            [
                ("异常", f"{type(exc).__name__}: {exc}" if exc else "(无)"),
                ("traceback", tb),
                ("请求体", json.dumps(data.get("payload"), ensure_ascii=False, indent=2) if data.get("payload") else "(无)"),
            ],
        )
        path = self.rec.save_failure(self.endpoint.get().strip(), self.current_job or "", report)
        self.last_report = report
        self.log(f"!! {type(exc).__name__}: {exc}", "err")
        self.log(f"!! 完整报告已写入：{path}", "err")
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", report)

    def _on_submitted(self, data: dict) -> None:
        body, status, text = data["body"], data["status"], data["text"]
        job_id = body.get("id") if isinstance(body, dict) else None
        job_status = body.get("status") if isinstance(body, dict) else None
        self.current_job = job_id
        self.log(f"HTTP {status} · job={job_id} · status={job_status} · {data['elapsed']:.1f}s "
                 f"({'/runsync' if data['sync'] else '/run'})")

        if status >= 400:
            self._report_http_failure(data, status, text, job_id)
            return

        self.status_line.configure(text=f"job {job_id} · {job_status} · {data['elapsed']:.1f}s")
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", json.dumps(body, ensure_ascii=False, indent=2)[:20000])

        if job_status in TERMINAL:
            self._finish_job(body, data, job_id, job_status)
        elif not data["sync"] and job_id:
            self.log("异步已提交，开始轮询…")
            self._start_poll(job_id)
        else:
            self._set_busy(False)

    def _on_status(self, data: dict) -> None:
        body, status, text = data["body"], data["status"], data["text"]
        job_status = body.get("status") if isinstance(body, dict) else None
        self.log(f"状态：{job_status}  (HTTP {status})")
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", json.dumps(body, ensure_ascii=False, indent=2)[:20000])
        if status >= 400:
            self._report_http_failure(data, status, text, self.current_job)
        elif job_status in TERMINAL:
            self._finish_job(body, data, self.current_job, job_status)
        else:
            self._set_busy(False)

    def _report_http_failure(self, data: dict, status: int, text: str, job_id) -> None:
        self._set_busy(False)
        report = build_failure_report(
            f"HTTP {status} —— 请求被拒",
            [
                ("URL", data.get("url", "")),
                ("HTTP 状态", str(status)),
                ("响应体（原样）", text),
                ("请求体（原样）", json.dumps(data.get("payload"), ensure_ascii=False, indent=2) if data.get("payload") else "(无)"),
            ],
        )
        path = self.rec.save_failure(self.endpoint.get().strip(), job_id or "", report)
        self.last_report = report
        self.log(f"!! HTTP {status}，响应体：{text[:500]}", "err")
        self.log(f"!! 报告已写入：{path}", "err")

    def _start_poll(self, job_id: str) -> None:
        ep, key = self.endpoint.get().strip(), self.key.get().strip()
        try:
            interval = max(1, int(self.poll_interval.get() or 4))
        except ValueError:
            interval = 4

        def work():
            while not self.stop_poll.is_set():
                time.sleep(interval)
                try:
                    body, text, status, elapsed, url = self.api.job_status(ep, key, job_id)
                except Exception as e:  # noqa: BLE001
                    self.q.put(("client_error", dict(exc=e, payload=None, where="轮询状态时")))
                    return
                st = body.get("status") if isinstance(body, dict) else None
                self.q.put(("status" if st in TERMINAL or status >= 400 else "log",
                            dict(body=body, text=text, status=status, elapsed=elapsed, url=url, payload=None)
                            if st in TERMINAL or status >= 400 else f"轮询：{st}"))
                if st in TERMINAL or status >= 400:
                    return

        threading.Thread(target=work, daemon=True).start()

    def _finish_job(self, body: dict, data: dict, job_id, job_status: str) -> None:
        exec_ms = body.get("executionTime")
        delay_ms = body.get("delayTime")
        out = body.get("output") or {}
        imgs = out.get("images") or []
        warn = out.get("errors") or []
        self.status_line.configure(
            text=f"job {job_id} · {job_status} · 执行 {exec_ms}ms · 排队 {delay_ms}ms · 图片 {len(imgs)} 张")

        if job_status == "COMPLETED" and imgs:
            self.log(f"✓ COMPLETED · 执行 {exec_ms}ms · 排队 {delay_ms}ms · {len(imgs)} 张图", "ok")
            for w in warn:
                self.log(f"  warn: {w}")
            self._show_image(imgs[0])
            self._set_busy(False)
            return

        # 失败 / 超时 / 完成但没图：抓现场
        self.log(f"!! job {job_status}（执行 {exec_ms}ms）—— 开始收集失败现场", "err")
        err = body.get("error") or out.get("error") or "(响应里没有 error 字段)"
        self.log(f"!! error: {str(err)[:600]}", "err")

        sections = [
            ("URL", data.get("url", f"{ENDPOINT_API}/{self.endpoint.get()}/status/{job_id}")),
            ("job", json.dumps({k: body.get(k) for k in
                                ("id", "status", "executionTime", "delayTime", "workerId", "error", "output")},
                               ensure_ascii=False, indent=2)[:20000]),
            ("output.errors", json.dumps(warn, ensure_ascii=False, indent=2)),
            ("error 全文", str(err)),
            ("请求体（原样）", json.dumps(data.get("payload"), ensure_ascii=False, indent=2) if data.get("payload") else "(本次没带，见 logs\\req\\)"),
            ("响应体（原样）", data.get("text", "")),
        ]
        ep, key = self.endpoint.get().strip(), self.key.get().strip()
        if self.grab_logs.get():
            self.log("正在抓 worker 日志（状态会骗人，日志才作数）…")
            sections += self._collect_worker_evidence(ep, key, prefix="失败现场")
        report = build_failure_report(f"job {job_status} —— {self.engine.get()} @ {ep}", sections)
        path = self.rec.save_failure(ep, job_id or "", report)
        self.last_report = report
        self.log(f"!! 完整报告已写入：{path}", "err")
        self.raw_out.delete("1.0", "end")
        self.raw_out.insert("end", report)
        self._set_busy(False)

    # ------------------------------------------------------------ 图片
    def _show_image(self, item: dict) -> None:
        data = item.get("data") or ""
        if not data:
            self.preview_label.configure(text=f"(无图片数据) {item.get('filename')}", image="")
            return
        try:
            raw = base64.b64decode(data)
        except Exception as e:  # noqa: BLE001
            self.preview_label.configure(text=f"(base64 解不开: {e})", image="")
            return
        self._last_image_bytes = raw
        self._last_image_name = item.get("filename") or "output.png"
        outdir = OUTPUTS
        outdir.mkdir(exist_ok=True)
        saved = outdir / f"{self.current_job or stamp()}_{self._last_image_name}"
        saved.write_bytes(raw)
        self.log(f"图片已保存：{saved}  ({len(raw)} bytes)")
        try:
            from PIL import Image, ImageTk
            im = Image.open(BytesIO(raw))
            im.thumbnail((560, 560))
            self.preview_img = ImageTk.PhotoImage(im)
            self.preview_label.configure(image=self.preview_img, text=f"{im.width}x{im.height}")
        except Exception as e:  # noqa: BLE001
            self.log(f"(预览失败，图已存盘: {e})")

    def _save_image(self) -> None:
        raw = getattr(self, "_last_image_bytes", None)
        if not raw:
            messagebox.showinfo("没有图", "还没有可保存的图片")
            return
        name = getattr(self, "_last_image_name", "output.png")
        p = filedialog.asksaveasfilename(defaultextension=Path(name).suffix or ".png",
                                         initialfile=name)
        if p:
            Path(p).write_bytes(raw)
            self.log(f"另存为：{p}")

    # ------------------------------------------------------------ busy
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_send, self.btn_preview):
            b.configure(state=state)
        self.btn_status.configure(state="normal" if self.current_job and not busy else "disabled")
        self.btn_cancel.configure(state="normal" if self.current_job and busy else "disabled")
        self.configure(cursor="watch" if busy else "")


def main() -> int:
    ensure_dirs()
    if "--selftest" in sys.argv:
        return selftest()
    if "--apitest" in sys.argv:
        return apitest()
    app = App()
    app.mainloop()
    return 0


def apitest() -> int:
    """用假 key 真打一次 API，把「请求 → 报错 → 抓 worker 日志 → 写失败报告」整条链路走通。

    不需要真 key，也不会碰到任何 endpoint：这一步只验证报错留档机制本身是好的。
    """
    rec = Recorder()
    api = Api(rec)
    ep = "000000000000000"
    key = "rpa_" + "x" * 24
    payload = {"input": {"workflow": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}}}

    print("[1] submit（假 key，期望 4xx）")
    status = None
    try:
        body, text, status, elapsed, url = api.submit(ep, key, payload, sync=True, timeout=30)
        print(f"    HTTP {status}  {elapsed:.2f}s  {text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"    异常：{type(e).__name__}: {e}")

    print("[2] workers 列表")
    w = api.workers(ep, key)
    print("    " + json.dumps(w, ensure_ascii=False)[:200])

    print("[3] worker 日志（假 worker id）")
    lurl, llines = api.worker_logs(ep, key, "nope", tail=5, max_seconds=5)
    print("    " + llines[0][:200])

    print("[4] 组装并落盘失败报告")
    report = build_failure_report(
        "apitest —— 故意用假 key 触发失败，验证留档链路",
        [
            ("URL", url if status is not None else "(没发出去)"),
            ("HTTP 状态", str(status)),
            ("响应体（原样）", text if status is not None else "(无)"),
            ("请求体（原样）", json.dumps(payload, ensure_ascii=False, indent=2)),
            ("worker 列表", json.dumps(w, ensure_ascii=False, indent=2)),
            ("worker 日志", "\n".join(llines)),
        ],
    )
    p = rec.save_failure(ep, "nojob", report)
    print(f"    {p}")
    print("-" * 70)
    print("\n".join(report.splitlines()[:24]))
    print("-" * 70)
    print("APITEST OK")
    return 0


def selftest() -> int:
    """不开窗口，验证：payload 构造（含图生图上传） + 界面对象能建起来 + 目录落盘。"""
    ensure_dirs()
    rec = Recorder()
    ok = True

    from PIL import Image
    tmp = LOGS / "_selftest_ref.png"
    Image.new("RGB", (64, 64), (120, 30, 200)).save(tmp)

    cases = {
        "anime t2i": argparse.Namespace(which="anime", prompt="p", negative="", image=None,
                                        width=256, height=256, steps=4, cfg=4.0, denoise=0.6,
                                        model=DEFAULTS["anima"]["model"], turbo=False,
                                        turbo_lora=DEFAULTS["anima"]["turbo_lora"], seed=1),
        "anime i2i": argparse.Namespace(which="anime", prompt="p", negative="", image=str(tmp),
                                        width=256, height=256, steps=4, cfg=4.0, denoise=0.6,
                                        model=DEFAULTS["anima"]["model"], turbo=False,
                                        turbo_lora=DEFAULTS["anima"]["turbo_lora"], seed=1),
        "qwen t2i": argparse.Namespace(which="qwen", prompt="p", negative="", image=[],
                                       width=256, height=256, resolution=1024, steps=4, cfg=1.0,
                                       model=DEFAULTS["qwen21"]["model"], seed=1),
        "qwen edit x2": argparse.Namespace(which="qwen", prompt="<image1> <image2>", negative="",
                                           image=[str(tmp), str(tmp)], width=256, height=256,
                                           resolution=1024, steps=4, cfg=1.0,
                                           model=DEFAULTS["qwen21"]["model"], seed=1),
    }
    for name, ns in cases.items():
        p = genpic.build_anima(ns) if ns.which == "anime" else genpic.build_qwen(ns)
        n_img = len(p["input"].get("images", []))
        n_nodes = len(p["input"]["workflow"])
        # 关键断言：图生图必须把图传到 input.images，且 LoadImage 指向上传后的文件名
        if ns.which == "anime" and ns.image:
            li = [v for v in p["input"]["workflow"].values() if v.get("class_type") == "LoadImage"]
            fn = p["input"]["images"][0]["name"] if n_img else None
            good = n_img == 1 and li and li[0]["inputs"]["image"] == fn
            ok &= bool(good)
            print(f"  {name:14s} images={n_img} nodes={n_nodes} LoadImage={li[0]['inputs']['image']!r} "
                  f"upload_name={fn!r}  {'OK' if good else 'FAIL'}")
        else:
            print(f"  {name:14s} images={n_img} nodes={n_nodes}  OK")
    tmp.unlink(missing_ok=True)

    app = App()          # 只建对象，不进 mainloop
    app.update_idletasks()

    # 默认值 / 尺寸控件 / 文本框是否都到位
    print(f"  默认 steps={app.steps.get()} cfg={app.cfgv.get()} "
          f"prompt={len(app._get_text(app.prompt_text))} 字符 "
          f"width={app.dim_w.value()} height={app.dim_h.value()}")
    ok &= app.steps.get() not in ("", "None") and app.cfgv.get() not in ("", "None")
    ok &= len(app._get_text(app.prompt_text)) > 0
    ok &= app.dim_w.value() == 1024

    for raw, want in (("1000", 1024), ("100", DIM_MIN), ("9999", DIM_MAX), ("512", 512), ("abc", 1024)):
        app.width.set(raw)
        got = app.dim_w.value()
        flag = "OK" if got == want else "FAIL"
        ok &= got == want
        print(f"  宽度规整 {raw!r:8s} -> {got:5d} (期望 {want})  {flag}")
    app.width.set("1024")

    p = app._build_payload()
    w = [v["inputs"]["width"] for v in p["input"]["workflow"].values()
         if v.get("class_type") == "EmptyLatentImage"]
    print(f"  用界面默认值构造 payload：nodes={len(p['input']['workflow'])} EmptyLatentImage.width={w}")
    ok &= w == [1024]
    app.destroy()
    print(f"  界面对象构建 OK；日志目录 {LOGS}")
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
