# -*- coding: utf-8 -*-
"""图形界面（tkinter）：填 token → 刷新任务 → 一键刷题，日志与进度实时可见。

线程模型：所有网络/答题都跑在后台线程，界面更新统一走 root.after 回到主线程，
「停止」按钮通过 threading.Event 通知引擎在本题收尾后退出。
"""

from __future__ import annotations

import base64
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

import requests

from . import net
from .bank import Bank
from .client import VocabgoClient, VocabgoError
from .llm import (
    PROVIDER_PRESETS,
    guess_models,
    load_llm_from_config,
    preset_choices,
    provider_from_label,
    provider_label,
    thinking_from_label,
    thinking_label,
)
from .paths import app_dir
from .paths import bank_path as default_bank_path
from .paths import config_path
from .qrlogin import (
    LoginResult,
    build_wechat_login_url,
    extract_wechat_code,
    fetch_captcha,
    load_existing_token,
    login_and_save,
    login_with_password,
    login_with_user_id,
    login_with_wechat_code,
    open_file,
    render_qr_png,
    token_alive,
    token_expiry_hint,
)
from .settings import apply_recommended, load_settings, recommended_lines, resolve_llm, save_settings
from .solver import Solver, select_words
from .vision import (
    VISION_BACKENDS,
    backend_label,
    backend_value,
    load_reader_from_config,
    probe_backend,
    resolve_tesseract,
)

APP_TITLE = "词达人自动答题工具"


class VocabgoApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = load_settings()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.tasks: list[dict[str, Any]] = []
        self._last_provider = ""
        self._models_loaded = False
        self._autosave_ready = False  # 首屏填控件期间别触发自动保存
        self._log_job: str | None = None
        self.login_window: tk.Toplevel | None = None
        self._captcha_req_no = ""

        root.title(APP_TITLE)
        self._build_ui()
        self._apply_config_to_widgets()
        self._refresh_status()
        self._fit_window()
        self._log_job = root.after(150, self._drain_log)
        root.bind("<Destroy>", self._on_destroy, add="+")
        root.protocol("WM_DELETE_WINDOW", self._on_close)  # 关窗前落盘，输入过的东西不会丢
        if self.var_llm_key.get().strip():
            root.after(900, self._auto_fetch_models)  # 已有密钥的话，开界面就自动拉一次模型
        self._autosave_ready = True
        self._announce_network()

    def _fit_window(self) -> None:
        """让窗口按内容自适应 —— 不手算尺寸，避免不同 DPI 下把右侧控件裁掉。"""
        self.root.update_idletasks()
        need_w = self.root.winfo_reqwidth() + 16
        need_h = self.root.winfo_reqheight() + 16
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # 内容比屏幕窄就直接用内容尺寸；否则压到屏幕内（宁可小一点也不越界）
        width = need_w if need_w <= screen_w - 40 else screen_w - 40
        height = need_h if need_h <= screen_h - 80 else screen_h - 80
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(need_w, screen_w - 40), min(need_h, screen_h - 80))

    # ── 界面搭建 ──
    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.notebook = notebook

        self.tab_run = ttk.Frame(notebook)
        self.tab_set = ttk.Frame(notebook)
        notebook.add(self.tab_run, text="  刷题  ")
        notebook.add(self.tab_set, text="  设置  ")

        self._build_run_tab()
        self._build_settings_tab()

        # 两个鉴权字段的灰色占位提示（留空即自动推算）
        self.hint_abc = self._attach_placeholder(self.entry_abc, self.var_abc, "留空即自动推算（md5(UA)）")
        self.hint_auth_v = self._attach_placeholder(self.entry_auth_v, self.var_auth_v, "留空即自动推算（md5(\"0\")）")
        self.hint_proxy = self._attach_placeholder(self.entry_proxy, self.var_proxy, "留空 = 直连")

        # 调试用：VOCABGO_GUI_TAB=settings 启动即停在设置页
        if os.environ.get("VOCABGO_GUI_TAB", "").strip().lower() in ("settings", "set", "设置", "2"):
            notebook.select(self.tab_set)

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=8)
        self.status = ttk.Label(self.root, anchor="w", text="就绪")
        self.status.pack(fill="x", padx=8, pady=(2, 6))

    def _build_run_tab(self) -> None:
        frame = self.tab_run

        # 账号（一个字段一行，任何 DPI 下都不会横向溢出）
        box = ttk.LabelFrame(frame, text="账号")
        box.pack(fill="x", padx=4, pady=4)
        ttk.Label(box, text="用户令牌").grid(row=0, column=0, sticky="w", padx=6, pady=(4, 0))
        self.var_token = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_token).grid(row=0, column=1, sticky="we", padx=6, pady=(4, 0))

        ttk.Label(box, text="abc 字段").grid(row=1, column=0, sticky="w", padx=6)
        self.var_abc = tk.StringVar()
        self.entry_abc = ttk.Entry(box, textvariable=self.var_abc)
        self.entry_abc.grid(row=1, column=1, sticky="we", padx=6)

        ttk.Label(box, text="authorization-v 字段").grid(row=2, column=0, sticky="w", padx=6)
        self.var_auth_v = tk.StringVar()
        self.entry_auth_v = ttk.Entry(box, textvariable=self.var_auth_v)
        self.entry_auth_v.grid(row=2, column=1, sticky="we", padx=6)

        buttons = ttk.Frame(box)
        buttons.grid(row=3, column=1, sticky="w", padx=6, pady=(2, 2))
        ttk.Button(buttons, text="登录获取令牌", command=self.on_login).pack(side="left")
        ttk.Button(buttons, text="测试连接", command=self.on_check_token).pack(side="left", padx=6)
        ttk.Button(buttons, text="说明", command=self.on_account_help).pack(side="left")

        ttk.Label(
            box,
            text="abc 与 authorization-v 留空即可（上面灰字就是提示），抓包填了真值则优先用真值",
            foreground="#52606d",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        box.columnconfigure(1, weight=1)

        # 任务
        box2 = ttk.LabelFrame(frame, text="任务")
        box2.pack(fill="both", expand=True, padx=4, pady=4)
        row = ttk.Frame(box2)
        row.pack(fill="x", padx=6, pady=4)
        self.var_kind = tk.StringVar(value="class")
        ttk.Radiobutton(row, text="班级任务", value="class", variable=self.var_kind, command=self.on_kind_change).pack(side="left")
        ttk.Radiobutton(row, text="自学任务", value="study", variable=self.var_kind, command=self.on_kind_change).pack(side="left", padx=8)
        ttk.Label(row, text="课程").pack(side="left", padx=(16, 2))
        self.var_course = tk.StringVar(value="CET4_v2")
        ttk.Entry(row, textvariable=self.var_course, width=14).pack(side="left")
        ttk.Button(row, text="刷新任务", command=self.on_refresh_tasks).pack(side="left", padx=12)

        self.task_list = tk.Listbox(box2, height=7, activestyle="dotbox")
        self.task_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        row2 = ttk.Frame(box2)
        row2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(row2, text="练习轮数").pack(side="left")
        self.var_loop = tk.StringVar(value="3")
        spin_loop = ttk.Spinbox(row2, from_=1, to=20, width=4, textvariable=self.var_loop)
        spin_loop.pack(side="left", padx=(2, 12))
        spin_loop.bind("<FocusOut>", lambda _e: self._autosave("练习轮数"))
        spin_loop.configure(command=lambda: self._autosave("练习轮数"))
        ttk.Label(row2, text="满分阈值").pack(side="left")
        self.var_max_score = tk.StringVar(value="10")
        spin_score = ttk.Spinbox(row2, from_=1, to=10, width=4, textvariable=self.var_max_score)
        spin_score.pack(side="left", padx=(2, 12))
        spin_score.bind("<FocusOut>", lambda _e: self._autosave("满分阈值"))
        spin_score.configure(command=lambda: self._autosave("满分阈值"))
        self.var_probe = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="试答案兜底", variable=self.var_probe, command=lambda: self._autosave("试答案开关")).pack(side="left", padx=6)
        self.var_ocr = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="读图", variable=self.var_ocr, command=lambda: self._autosave("读图开关")).pack(side="left", padx=6)

        self.btn_start = ttk.Button(row2, text="▶ 开始刷题", command=self.on_start)
        self.btn_start.pack(side="right")
        self.btn_stop = ttk.Button(row2, text="⏹ 停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="right", padx=6)

        # 日志
        box3 = ttk.LabelFrame(frame, text="日志")
        box3.pack(fill="both", expand=True, padx=4, pady=(4, 6))
        self.log_text = tk.Text(box3, height=12, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(box3, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_settings_tab(self) -> None:
        frame = self.tab_set

        # LLM
        llm_box = ttk.LabelFrame(frame, text="大模型（答题兜底 + 读图）")
        llm_box.pack(fill="x", padx=4, pady=6)
        ttk.Label(llm_box, text="服务商").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_provider = tk.StringVar(value=provider_label("deepseek"))
        self._provider_pairs = preset_choices()  # [(内部标识, 中文名)]
        self.cbo_provider = ttk.Combobox(
            llm_box,
            textvariable=self.var_provider,
            values=[text for _, text in self._provider_pairs],
            state="readonly",
            width=22,
        )
        self.cbo_provider.grid(row=0, column=1, sticky="w")
        self.cbo_provider.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self.lbl_provider_hint = ttk.Label(llm_box, text=PROVIDER_PRESETS["deepseek"]["label"])
        self.lbl_provider_hint.grid(row=0, column=2, columnspan=2, sticky="w", padx=8)

        ttk.Label(llm_box, text="接口密钥").grid(row=1, column=0, sticky="w", padx=6)
        self.var_llm_key = tk.StringVar()
        self.entry_key = ttk.Entry(llm_box, textvariable=self.var_llm_key, width=40, show="•")
        self.entry_key.grid(row=1, column=1, columnspan=2, sticky="we", padx=(6, 4))
        self.entry_key.bind("<FocusOut>", lambda _event: self._on_key_committed())
        self.entry_key.bind("<Return>", lambda _event: self._on_key_committed())
        ttk.Button(llm_box, text="💾 保存配置", command=self.on_save_settings).grid(row=1, column=3, sticky="w", padx=(0, 6))

        ttk.Label(llm_box, text="接口地址").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.var_llm_url = tk.StringVar()
        ttk.Entry(llm_box, textvariable=self.var_llm_url, width=40).grid(row=2, column=1, columnspan=3, sticky="we", padx=6)

        ttk.Label(llm_box, text="答题模型").grid(row=3, column=0, sticky="w", padx=6)
        self.var_llm_model = tk.StringVar()
        self.cbo_llm_model = ttk.Combobox(llm_box, textvariable=self.var_llm_model, width=22)
        self.cbo_llm_model.grid(row=3, column=1, sticky="we", padx=6)
        self.cbo_llm_model.bind("<<ComboboxSelected>>", lambda _e: self._autosave("答题模型"))
        ttk.Label(llm_box, text="读图模型").grid(row=3, column=2, sticky="e")
        self.var_vision_model = tk.StringVar()
        self.cbo_vision_model = ttk.Combobox(llm_box, textvariable=self.var_vision_model, width=22)
        self.cbo_vision_model.grid(row=3, column=3, sticky="we", padx=6)
        self.cbo_vision_model.bind("<<ComboboxSelected>>", lambda _e: self._autosave("读图模型"))

        fetch_row = ttk.Frame(llm_box)
        fetch_row.grid(row=4, column=1, columnspan=3, sticky="w", padx=6, pady=(2, 0))
        ttk.Button(fetch_row, text="拉取模型列表", command=self.on_fetch_models).pack(side="left")
        self.lbl_model_hint = ttk.Label(fetch_row, text="填好接口密钥后会自动拉取；也可以点左边按钮手动拉")
        self.lbl_model_hint.pack(side="left", padx=8)
        self.lbl_save_state = ttk.Label(fetch_row, text="", foreground="#2e7d32")
        self.lbl_save_state.pack(side="left")

        ttk.Label(llm_box, text="思考模式").grid(row=5, column=0, sticky="w", padx=6, pady=(0, 6))
        self.var_thinking = tk.StringVar(value=thinking_label("off"))
        self.cbo_thinking = ttk.Combobox(
            llm_box,
            textvariable=self.var_thinking,
            values=[thinking_label(key) for key in ("auto", "off", "on")],
            state="readonly",
            width=20,
        )
        self.cbo_thinking.grid(row=5, column=1, sticky="w")
        self.cbo_thinking.bind("<<ComboboxSelected>>", lambda _e: self._autosave("思考模式"))
        ttk.Label(
            llm_box,
            text="留空即用预设",
        ).grid(row=5, column=2, columnspan=2, sticky="w", padx=8)
        llm_box.columnconfigure(3, weight=1)

        # 读图
        ocr_box = ttk.LabelFrame(frame, text="读图（题干/选项是图片时）")
        ocr_box.pack(fill="x", padx=4, pady=6)
        ttk.Label(ocr_box, text="读图方式").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_backend = tk.StringVar(value=backend_label("auto"))
        self.cbo_backend = ttk.Combobox(
            ocr_box,
            textvariable=self.var_backend,
            values=[backend_label(key) for key in VISION_BACKENDS],
            state="readonly",
            width=28,
        )
        self.cbo_backend.grid(row=0, column=1, sticky="w")
        self.cbo_backend.bind("<<ComboboxSelected>>", lambda _e: self._autosave("读图方式"))
        self.lbl_tesseract = ttk.Label(ocr_box, text="")
        self.lbl_tesseract.grid(row=0, column=2, columnspan=2, sticky="w", padx=8)
        ttk.Button(ocr_box, text="读图体检", command=self.on_vision_check).grid(row=0, column=4, padx=6)
        ocr_box.columnconfigure(3, weight=1)

        # 节奏
        pace_box = ttk.LabelFrame(frame, text="节奏与文件")
        pace_box.pack(fill="x", padx=4, pady=6)
        ttk.Label(pace_box, text="作答间隔（秒）").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_pace_lo = tk.StringVar(value="1.0")
        self.entry_pace_lo = ttk.Entry(pace_box, textvariable=self.var_pace_lo, width=6)
        self.entry_pace_lo.grid(row=0, column=1, sticky="w")
        self.entry_pace_lo.bind("<FocusOut>", lambda _e: self._autosave("作答间隔"))
        ttk.Label(pace_box, text="~").grid(row=0, column=2)
        self.var_pace_hi = tk.StringVar(value="2.6")
        self.entry_pace_hi = ttk.Entry(pace_box, textvariable=self.var_pace_hi, width=6)
        self.entry_pace_hi.grid(row=0, column=3, sticky="w")
        self.entry_pace_hi.bind("<FocusOut>", lambda _e: self._autosave("作答间隔"))
        ttk.Label(pace_box, text="太快更容易被风控盯上").grid(row=0, column=4, sticky="w", padx=8)

        ttk.Label(pace_box, text="配置文件").grid(row=1, column=0, sticky="w", padx=6)
        self.lbl_config_path = ttk.Label(pace_box, text=self._short(config_path(), 3))
        self.lbl_config_path.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Label(pace_box, text="题库").grid(row=2, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(pace_box, text=self._short(default_bank_path(), 3)).grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(pace_box, text="代理").grid(row=3, column=0, sticky="w", padx=6, pady=(0, 6))
        self.var_proxy = tk.StringVar()
        self.entry_proxy = ttk.Entry(pace_box, textvariable=self.var_proxy, width=26)
        self.entry_proxy.grid(row=3, column=1, columnspan=2, sticky="we", padx=6, pady=(0, 6))
        self.entry_proxy.bind("<FocusOut>", lambda _e: self._autosave("代理"))
        self.var_sys_proxy = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pace_box, text="跟随系统代理", variable=self.var_sys_proxy, command=lambda: self._autosave("代理")
        ).grid(row=3, column=3, sticky="w", padx=6)
        ttk.Label(pace_box, text="留空 = 直连（推荐）", foreground="#52606d").grid(row=3, column=4, sticky="w", padx=8)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=8, pady=8)
        ttk.Button(actions, text="⭐ 一键填入推荐配置", command=self.on_apply_recommended).pack(side="left")
        ttk.Button(actions, text="💾 保存设置", command=self.on_save_settings).pack(side="left", padx=8)
        ttk.Button(actions, text="大模型体检", command=self.on_llm_check).pack(side="left")
        ttk.Button(actions, text="打开程序目录", command=lambda: open_file(app_dir())).pack(side="left", padx=8)

    def _apply_network(self) -> None:
        """把界面上的代理设置同步给网络层（默认直连，忽略系统代理）。"""
        network = self.config.setdefault("network", {})
        net.configure(str(network.get("proxy") or ""), bool(network.get("use_system_proxy")))

    def _announce_token_age(self) -> None:
        """启动时看一眼令牌多老了 —— 词达人令牌约 6 小时过期，提前说一声比刷到一半断掉强。"""
        hint = token_expiry_hint(self.config)
        if hint:
            self.log(hint)

    def _announce_network(self) -> None:
        """启动时把代理策略讲清楚 —— 电脑上残留代理是「全部请求失败」的头号原因。"""
        self._apply_network()
        if net.proxies():
            self.log(f"🌐 网络：使用指定代理 {net.proxies()}")
            self._announce_token_age()
            return
        if net.trust_env():
            self.log(f"🌐 网络：跟随系统代理（检测到 {net.detect_proxy() or '无'}）")
            self._announce_token_age()
            return
        detected = net.detect_proxy()
        if detected:
            self.log(f"🌐 网络：直连（已忽略系统/环境里的代理 {detected}）")
            self.log("   需要走代理的话，到「设置」页填代理地址或勾选「跟随系统代理」")
        else:
            self.log("🌐 网络：直连")
        self._announce_token_age()

    def _on_provider_selected(self, _event: Any = None) -> None:
        self._on_provider_change()
        self._autosave("服务商")

    def _autosave(self, reason: str = "") -> None:
        """改动即存：输入框失去焦点、下拉框选择、勾选框切换都会走到这里。"""
        if not self._autosave_ready:
            return
        try:
            self.collect_config()
            self._apply_network()
            path = save_settings(self.config)
            self._mark_saved(f"已自动保存（{reason}）" if reason else "已自动保存")
            self._refresh_status()
        except Exception as exc:
            self.log(f"⚠️ 自动保存失败：{exc}（点「💾 保存配置」重试）")

    def _mark_saved(self, text: str, *, also_log: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.lbl_save_state.configure(text=f"✅ {text} {stamp}")
        if also_log:
            self.log(f"💾 {text} → {config_path()}")

    def _on_key_committed(self) -> None:
        """密钥填完（失焦或回车）：先存下来，再去拉模型列表。"""
        self._autosave("接口密钥")
        self._auto_fetch_models()

    def _on_close(self) -> None:
        try:
            if self._autosave_ready:
                self.collect_config()
                save_settings(self.config)
        except Exception:
            pass
        if self._log_job:
            try:
                self.root.after_cancel(self._log_job)
            except Exception:
                pass
            self._log_job = None
        self.cancel.set()
        self.root.destroy()

    def _on_destroy(self, event: Any) -> None:
        """窗口被销毁（不管是点关闭还是被外部 destroy）时，把待执行的定时回调收掉。"""
        if event.widget is not self.root or not self._log_job:
            return
        try:
            self.root.after_cancel(self._log_job)
        except Exception:
            pass
        self._log_job = None

    # ── 配置 <-> 控件 ──
    def _on_provider_change(self) -> None:
        """换服务商就自动带出接口地址与模型名（都能手改）；服务商没变时只补空值。"""
        provider = provider_from_label(self.var_provider.get())
        preset = PROVIDER_PRESETS.get(provider) or {}
        self.lbl_provider_hint.configure(text=preset.get("label") or "自定义接口：地址和模型都要自己填")

        if provider == self._last_provider:
            if preset.get("url") and not self.var_llm_url.get().strip():
                self.var_llm_url.set(str(preset["url"]))  # 首次打开且配置里没写地址时补上
            return

        self._last_provider = provider
        if preset.get("url"):
            self.var_llm_url.set(str(preset["url"]))
        if preset.get("model"):
            self.var_llm_model.set(str(preset["model"]))
        if preset.get("vision_model"):
            self.var_vision_model.set(str(preset["vision_model"]))
        if preset.get("thinking"):
            self.var_thinking.set(thinking_label(str(preset["thinking"])))
        self.lbl_model_hint.configure(text="填好接口密钥后会自动拉取模型；也可以点左边按钮手动拉")

    def _current_helper(self):
        """按界面上当前填的值构造一个调用器（拉模型/体检都用它）。"""
        return load_llm_from_config(self.collect_config(), logger=self.log)

    def _apply_config_to_widgets(self) -> None:
        auth = self.config.get("auth") or {}
        self.var_token.set(str(auth.get("usertoken") or load_existing_token()))
        self.var_abc.set(str(auth.get("abc") or ""))
        self.var_auth_v.set(str(auth.get("authorization_v") or ""))

        llm = self.config.get("llm") or {}
        resolved = resolve_llm(self.config)  # 显示的必须是「真正会用的值」（含预设补全）
        provider = provider_from_label(str(llm.get("provider") or "deepseek"))
        self.var_provider.set(provider_label(provider))
        self._last_provider = provider  # 首屏不要被预设覆盖掉配置文件里的值
        self.var_llm_key.set(str(llm.get("key") or ""))
        self.var_llm_url.set(str(llm.get("url") or resolved["url"] or ""))
        self.var_llm_model.set(str(llm.get("model") or resolved["model"] or ""))
        self.var_vision_model.set(str(llm.get("vision_model") or resolved["vision_model"] or ""))
        self.var_thinking.set(thinking_label(resolved["thinking"] or "auto"))
        self._on_provider_change()

        vision = self.config.get("vision") or {}
        self.var_backend.set(backend_label(str(vision.get("backend") or "auto")))

        runtime = self.config.get("runtime") or {}
        pace = runtime.get("pace") or [1.0, 2.6]
        self.var_pace_lo.set(str(pace[0]))
        self.var_pace_hi.set(str(pace[1]))
        self.var_probe.set(bool(runtime.get("probe", True)))
        self.var_ocr.set(bool(runtime.get("ocr", True)))
        self.var_max_score.set(str(runtime.get("max_score") or 10))
        self.var_loop.set(str(runtime.get("loop") or 3))
        self.var_kind.set(str(runtime.get("kind") or "class"))

        network = self.config.get("network") or {}
        self.var_proxy.set(str(network.get("proxy") or ""))
        self.var_sys_proxy.set(bool(network.get("use_system_proxy", False)))
        self._apply_network()

    def collect_config(self) -> dict[str, Any]:
        """把界面上的值收集成配置字典（显示用中文，落盘一律写内部标识）。"""
        config = self.config
        config.setdefault("auth", {})["usertoken"] = self.var_token.get().strip()
        config["auth"]["abc"] = self.var_abc.get().strip()
        config["auth"]["authorization_v"] = self.var_auth_v.get().strip()

        llm = config.setdefault("llm", {})
        llm["provider"] = provider_from_label(self.var_provider.get().strip()) or "deepseek"
        llm["key"] = self.var_llm_key.get().strip()
        llm["url"] = self.var_llm_url.get().strip()
        llm["model"] = self.var_llm_model.get().strip()
        llm["vision_model"] = self.var_vision_model.get().strip()
        llm["thinking"] = thinking_from_label(self.var_thinking.get().strip())

        vision = config.setdefault("vision", {})
        vision["backend"] = backend_value(self.var_backend.get().strip())
        vision.setdefault("tesseract_cmd", "")

        study = config.setdefault("study", {})
        study["course_id"] = self.var_course.get().strip() or "CET4_v2"

        runtime = config.setdefault("runtime", {})
        try:
            lo = float(self.var_pace_lo.get())
            hi = float(self.var_pace_hi.get())
        except ValueError:
            lo, hi = 1.0, 2.6
        runtime["pace"] = [lo, hi]
        runtime["probe"] = bool(self.var_probe.get())
        runtime["ocr"] = bool(self.var_ocr.get())
        try:
            runtime["max_score"] = float(self.var_max_score.get() or 10)
            runtime["loop"] = max(1, int(float(self.var_loop.get() or 3)))
        except ValueError:
            runtime["max_score"] = runtime.get("max_score") or 10
            runtime["loop"] = runtime.get("loop") or 3
        runtime["kind"] = self.var_kind.get() or "class"
        runtime.setdefault("bank_path", "")
        runtime.setdefault("last_task_id", "")

        network = config.setdefault("network", {})
        network["proxy"] = self.var_proxy.get().strip()
        network["use_system_proxy"] = bool(self.var_sys_proxy.get())
        return config

    def on_account_help(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "怎么拿到用户令牌？\n\n"
            "方式一（推荐）：点「登录获取令牌」，用你的词达人账号／密码登录，\n"
            "  程序会自动把令牌写好。\n\n"
            "方式二：如果你只有「用户ID」，用第二种登录方式（需要填图形验证码）。\n\n"
            "方式三：自己抓包。跑 mitmdump -s tools/mitm_capture.py，让微信走代理，\n"
            "  打开词达人学生端，插件会把 usertoken / abc / authorization-v 自动写进配置。\n\n"
            "关于下面两个字段：\n"
            "· abc：平台反爬校验，值 = md5(你的客户端 UA)\n"
            "· authorization-v：同类版本标记，值固定为 md5(\"0\")\n"
            "  这两个留空即可 —— 程序会自己推算；抓包拿到真值则优先用真值。\n\n"
            "注意：刷题期间不要在手机或其它设备登录词达人，否则令牌会被刷新失效。",
        )

    def on_apply_recommended(self) -> None:
        """一键填入推荐配置：参数不用研究，填个密钥就能稳定跑。"""
        apply_recommended(self.config)
        self._apply_config_to_widgets()
        try:
            save_settings(self.config)
        except Exception as exc:
            self.log(f"⚠️ 保存失败：{exc}")
        self._mark_saved("已填入推荐配置")
        self.log("⭐ 推荐配置已写入（令牌与密钥保持不动）：")
        for line in recommended_lines():
            self.log(line)
        self.log("   下一步：填「接口密钥」→ 回到刷题页点「刷新任务」")
        self._refresh_status()

    def on_save_settings(self) -> None:
        try:
            config = self.collect_config()
            self._apply_network()
            path = save_settings(config)
            self._mark_saved("配置已保存", also_log=True)
            self.log(f"   下次打开会自动带出这些配置：服务商 / 接口地址 / 模型 / 密钥 / 读图方式 / 节奏")
            self._refresh_status()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")

    # ── 占位提示 ──
    def _attach_placeholder(self, entry: ttk.Entry, var: tk.StringVar, text: str) -> ttk.Label:
        """给输入框加灰色占位提示。

        关键：提示文字放在**独立浮层**里，不写进 StringVar ——
        否则它会跟着配置一起保存，还会被当成真值发进请求头。
        """
        hint = ttk.Label(self.root, text=text, foreground="#9aa5b1", background="#ffffff")
        try:
            hint.place(in_=entry, x=6, rely=0.5, anchor="w")
        except Exception:
            return hint

        def refresh() -> None:
            try:
                empty = not var.get().strip()
                focused = self.root.focus_get() is entry
            except Exception:
                return
            if empty and not focused:
                hint.place(in_=entry, x=6, rely=0.5, anchor="w")
                hint.lift()
            else:
                hint.place_forget()

        hint.bind("<Button-1>", lambda _event: entry.focus_set())
        var.trace_add("write", lambda *_args: refresh())
        entry.bind("<FocusIn>", lambda _event: refresh(), add="+")
        entry.bind("<FocusOut>", lambda _event: refresh(), add="+")
        hint._refresh = refresh  # type: ignore[attr-defined]  # 供测试/外部触发
        refresh()
        return hint

    # ── 日志与状态 ──
    def log(self, message: str) -> None:
        self.log_queue.put(str(message))

    def _drain_log(self) -> None:
        wrote = False
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                wrote = True
        except queue.Empty:
            pass
        if wrote:
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        try:
            self._log_job = self.root.after(150, self._drain_log)
        except tk.TclError:
            self._log_job = None  # 窗口已经销毁，收工

    def ui(self, fn: Callable[..., Any], *args: Any) -> None:
        """从后台线程安全地更新界面。"""
        self.root.after(0, lambda: fn(*args))

    def _short(self, path: Any, parts: int = 2) -> str:
        """把长路径缩成最后两段显示，避免把窗口撑宽（完整路径在设置页里看）。"""
        try:
            pieces = list(Path(path).parts)
        except Exception:
            return str(path)
        return str(Path(*pieces[-parts:])) if len(pieces) > parts else str(path)

    def _refresh_status(self) -> None:
        token = self.var_token.get().strip()
        bank_file = default_bank_path()
        count = len(Bank(bank_file)) if bank_file.exists() else 0
        state = "已配置" if token else "未配置"
        self.status.configure(text=f"用户令牌：{state}　|　题库：{count} 条　|　配置：{self._short(config_path())}")
        exe = resolve_tesseract()
        self.lbl_tesseract.configure(text=f"本地识别程序：{'已就绪' if exe else '未找到（可改用大模型读图）'}")

    def _busy(self, busy: bool, start_label: str = "▶ 开始刷题") -> None:
        self.btn_start.configure(state="disabled" if busy else "normal", text="运行中…" if busy else start_label)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress.configure(value=0)

    def _run_in_thread(self, target: Callable[[], None], *, busy: bool = True) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "还有任务在跑，先停止它")
            return
        self.cancel.clear()
        if busy:
            self._busy(True)
        self.worker = threading.Thread(target=self._guard(target), daemon=True)
        self.worker.start()

    def _guard(self, target: Callable[[], None]) -> Callable[[], None]:
        def wrapper() -> None:
            try:
                target()
            except RuntimeError as exc:  # 含 VocabgoError 与授权流程的 RuntimeError
                self.log(f"❌ {exc}")
            except SystemExit as exc:
                self.log(f"❌ {exc}")
            except requests.RequestException as exc:
                self.log(f"❌ {net.friendly_error(exc)}")
            except ValueError as exc:  # JSONDecodeError 之类
                self.log(f"❌ {net.friendly_error(exc)}")
            except Exception as exc:  # 后台线程绝不能把异常吞掉又不说
                self.log(f"❌ {type(exc).__name__}: {exc}")
            finally:
                self.ui(self._busy, False)

        return wrapper

    # ── 账号操作 ──
    def _client(self) -> VocabgoClient:
        self.collect_config()
        self._apply_network()  # 建连接前先把代理策略同步好
        token = self.var_token.get().strip()
        if not token:
            raise VocabgoError("还没填 UserToken：点「扫码授权」，或抓包后粘进输入框")
        return VocabgoClient(
            usertoken=token,
            abc=self.var_abc.get().strip(),
            auth_v=self.var_auth_v.get().strip(),
        )

    def on_check_token(self) -> None:
        def task() -> None:
            client = self._client()
            if client.token_alive():
                self.log("✅ 用户令牌有效")
            else:
                self.log("❌ 用户令牌已失效，请重新「扫码授权」")
            self.ui(self._refresh_status)

        self._run_in_thread(task, busy=False)

    def on_login(self) -> None:
        """打开「获取用户令牌」对话框：账号密码登录 / 用户ID+图形验证码。"""
        if self.login_window is not None and self.login_window.winfo_exists():
            self.login_window.lift()
            return

        window = tk.Toplevel(self.root)
        self.login_window = window
        window.title("获取用户令牌")
        window.resizable(False, False)
        try:
            window.transient(self.root)
        except Exception:
            pass

        # ── 方式一：账号密码 ──
        box1 = ttk.LabelFrame(window, text="方式一：账号密码登录（推荐）")
        box1.pack(fill="x", padx=12, pady=(12, 6))
        self.var_login_user = tk.StringVar()
        self.var_login_pass = tk.StringVar()
        ttk.Label(box1, text="用户名 / 学号").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(box1, textvariable=self.var_login_user, width=26).grid(row=0, column=1, sticky="we", padx=10, pady=4)
        ttk.Label(box1, text="密码").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        entry_pwd = ttk.Entry(box1, textvariable=self.var_login_pass, show="•", width=26)
        entry_pwd.grid(row=1, column=1, sticky="we", padx=10, pady=4)
        entry_pwd.bind("<Return>", lambda _e: self._do_password_login())
        ttk.Button(box1, text="登录", command=self._do_password_login).grid(row=2, column=1, sticky="e", padx=10, pady=(2, 8))
        box1.columnconfigure(1, weight=1)

        # ── 方式二：用户ID + 图形验证码 ──
        box2 = ttk.LabelFrame(window, text="方式二：用户ID + 图形验证码")
        box2.pack(fill="x", padx=12, pady=6)
        self.var_login_id = tk.StringVar()
        self.var_login_pass2 = tk.StringVar()
        self.var_login_code = tk.StringVar()
        ttk.Label(box2, text="用户ID").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(box2, textvariable=self.var_login_id, width=12).grid(row=0, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(box2, text="密码").grid(row=0, column=2, sticky="e", padx=6, pady=4)
        ttk.Entry(box2, textvariable=self.var_login_pass2, show="•", width=16).grid(row=0, column=3, sticky="we", padx=10, pady=4)
        ttk.Label(box2, text="验证码").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        entry_code = ttk.Entry(box2, textvariable=self.var_login_code, width=12)
        entry_code.grid(row=1, column=1, sticky="w", padx=10, pady=4)
        entry_code.bind("<Return>", lambda _e: self._do_id_login())
        self.lbl_captcha = ttk.Label(box2, text="点「换一张」取验证码", cursor="hand2")
        self.lbl_captcha.grid(row=1, column=2, sticky="e", padx=4, pady=4)
        self.lbl_captcha.bind("<Button-1>", lambda _e: self._refresh_captcha())
        ttk.Button(box2, text="换一张", command=self._refresh_captcha).grid(row=1, column=4, padx=8, pady=4)
        ttk.Button(box2, text="登录", command=self._do_id_login).grid(row=2, column=4, sticky="e", padx=8, pady=(2, 8))
        box2.columnconfigure(3, weight=1)
        self._captcha_req_no = ""

        # ── 方式三：微信扫码（不需要密码）──
        box3 = ttk.LabelFrame(window, text="方式三：微信扫码登录（不需要密码）")
        box3.pack(fill="x", padx=12, pady=6)
        wechat_url = build_wechat_login_url()
        qr_png = render_qr_png(wechat_url)
        if qr_png:
            qr_photo = tk.PhotoImage(data=base64.b64encode(qr_png).decode("ascii"))
            qr_label = ttk.Label(box3, image=qr_photo)
            qr_label.image = qr_photo  # type: ignore[attr-defined]  # 防 GC
            qr_label.grid(row=0, column=0, rowspan=3, padx=12, pady=10)
        else:
            ttk.Label(box3, text="（二维码组件缺失）", foreground="#b00").grid(row=0, column=0, padx=12, pady=10)

        ttk.Label(
            box3,
            text="① 手机微信扫码，或把「复制登录链接」发到电脑微信里点开（PC 上点开不用扫码）\n"
                 "② 页面打开后，点右上角「⋯」→「复制链接」\n"
                 "③ 回到这里点「从剪贴板读取并登录」——或者把链接粘到下面再点「确认登录」",
            justify="left",
        ).grid(row=0, column=1, sticky="w", padx=8, pady=(10, 4))
        self.var_wechat_url = tk.StringVar()
        entry_wechat = ttk.Entry(box3, textvariable=self.var_wechat_url, width=46)
        entry_wechat.grid(row=1, column=1, sticky="we", padx=8, pady=4)
        entry_wechat.bind("<Return>", lambda _e: self._do_wechat_login())

        buttons3 = ttk.Frame(box3)
        buttons3.grid(row=2, column=1, sticky="e", padx=8, pady=(0, 10))
        ttk.Button(buttons3, text="复制登录链接", command=lambda: self._copy_text(wechat_url)).pack(side="left", padx=4)
        ttk.Button(buttons3, text="从剪贴板读取并登录", command=self._wechat_login_from_clipboard).pack(side="left", padx=4)
        ttk.Button(buttons3, text="确认登录", command=self._do_wechat_login).pack(side="left", padx=4)
        box3.columnconfigure(1, weight=1)

        ttk.Label(
            window,
            text="三种方式任选一种即可。没有密码就扫码，登录失败就抓包（tools/mitm_capture.py）。",
            foreground="#52606d",
            justify="left",
        ).pack(anchor="w", padx=14, pady=(6, 10))
        ttk.Button(window, text="关闭", command=self._close_login_window).pack(anchor="e", padx=14, pady=(0, 12))

        window.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
        y = self.root.winfo_rooty() + 60
        window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        window.lift()
        window.focus_force()

    def _close_login_window(self) -> None:
        if self.login_window is not None and self.login_window.winfo_exists():
            self.login_window.destroy()
        self.login_window = None

    def _refresh_captcha(self) -> None:
        def task() -> None:
            try:
                req_no, image = fetch_captcha()
            except Exception as exc:
                self.log(f"❌ 取验证码失败：{exc}")
                return
            if not image:
                self.log("⚠️ 服务端没返回验证码图片")
                return
            self.ui(self._show_captcha, req_no, image)

        self._run_in_thread(task, busy=False)

    def _show_captcha(self, req_no: str, image_uri: str) -> None:
        self._captcha_req_no = req_no
        try:
            photo = tk.PhotoImage(data=image_uri.split(",", 1)[-1])
        except Exception as exc:
            self.log(f"⚠️ 验证码图片显示失败：{exc}")
            return
        self.lbl_captcha.configure(image=photo, text="")
        self.lbl_captcha.image = photo  # type: ignore[attr-defined]  # 防 GC
        self.log("🔢 验证码已显示，照着图填进「验证码」框")

    def _do_password_login(self) -> None:
        user = self.var_login_user.get().strip()
        password = self.var_login_pass.get()
        if not user or not password:
            messagebox.showwarning(APP_TITLE, "用户名和密码都要填")
            return
        self.log(f"🔑 正在用账号密码登录（{user}）…")

        def task() -> None:
            self._handle_login(login_with_password(user, password))

        self._run_in_thread(task, busy=False)

    def _copy_text(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("📋 登录链接已复制 —— 发给电脑微信（或手机微信）点开，再用页面上的「⋯ → 复制链接」把带 code 的链接拿回来")
        except Exception as exc:
            self.log(f"⚠️ 复制失败：{exc}")

    def _wechat_login_from_clipboard(self) -> None:
        """直接读剪贴板：用户复制完那条链接，点一下这个按钮就行，不用手动粘。"""
        try:
            raw = self.root.clipboard_get()
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, f"读不到剪贴板内容：{exc}\n\n请先在微信页面点「⋯ → 复制链接」。")
            return
        code = extract_wechat_code(raw)
        if not code:
            messagebox.showwarning(
                APP_TITLE,
                "剪贴板里没找到 code。\n\n请确认复制的是「打开后的那个页面」的链接（包含 code=xxxx 那段），"
                "而不是二维码那一步的链接。",
            )
            return
        self.var_wechat_url.set(raw.strip())
        self.log(f"📋 已从剪贴板读到链接，code={code[:8]}…")
        self._do_wechat_login()

    def _do_wechat_login(self) -> None:
        raw = self.var_wechat_url.get().strip()
        code = extract_wechat_code(raw)
        if not code:
            messagebox.showwarning(APP_TITLE, "没识别到链接里的 code。请把微信里复制到的那整条链接粘进来。")
            return
        self.log("🔑 正在用微信授权码换令牌…")

        def task() -> None:
            self._handle_login(login_with_wechat_code(code))

        self._run_in_thread(task, busy=False)

    def _do_id_login(self) -> None:
        user_id = self.var_login_id.get().strip()
        password = self.var_login_pass2.get()
        code = self.var_login_code.get().strip()
        if not user_id or not password or not code:
            messagebox.showwarning(APP_TITLE, "用户ID、密码、验证码都要填")
            return
        if not self._captcha_req_no:
            messagebox.showwarning(APP_TITLE, "请先点「换一张」获取验证码图片")
            return
        self.log(f"🔑 正在用用户ID 登录（ID={user_id}）…")

        def task() -> None:
            self._handle_login(login_with_user_id(user_id, password, code, self._captcha_req_no))

        self._run_in_thread(task, busy=False)

    def _handle_login(self, result: LoginResult) -> None:
        if not result.ok:
            self.log(f"❌ 登录失败：{result.message}")
            self.ui(self._on_login_failed, result.message)
            return
        path = login_and_save(result)
        self.ui(self.var_token.set, result.token)
        self.log(f"✅ 登录成功，令牌已写入 {path}")
        self.log(f"   令牌：{result.token[:10]}…（点「测试连接」可复验）")
        self.ui(self._refresh_status)
        self.ui(self._close_login_window)

    def _on_login_failed(self, message: str) -> None:
        messagebox.showerror(APP_TITLE, f"登录失败：{message}")
        self._refresh_captcha()  # 验证码是一次性的，失败后换一张

    # ── 任务操作 ──
    def on_kind_change(self) -> None:
        self.tasks = []
        self.task_list.delete(0, "end")

    def on_refresh_tasks(self) -> None:
        def task() -> None:
            client = self._client()
            kind = self.var_kind.get()
            if kind == "study":
                course = self.var_course.get().strip() or "CET4_v2"
                records = (client.study_task_list(course_id=course).get("data") or {}).get("task_list") or []
            else:
                records = (client.page_task(page=1, size=50, search_type="0").get("data") or {}).get("records") or []
            self.tasks = list(records)
            self.ui(self.task_list.delete, 0, "end")
            is_study = kind == "study"
            for index, item in enumerate(self.tasks):
                task_id = item.get("task_id")
                # 班级任务里 task_id<=0 说明这条还没在词达人里开始过（服务端没给编号），
                # 拿去请求会得到「加载任务信息失败」；自学任务的 -1 是正常的（会先创建），所以只标班级任务。
                unstarted = (not is_study) and self._is_unstarted(item)
                line = (
                    f"[{index}] {item.get('task_name')}　进度{item.get('progress')}%　得分{item.get('score')}"
                    f"　编号={task_id}" + ("　⚠️未开始（先在词达人里点开它）" if unstarted else "")
                )
                self.ui(self.task_list.insert, "end", line)
                self.log(f"📚 {line}")
            if not self.tasks:
                self.log("⚠️ 没查到任务（token 失效？或没有进行中的任务）")
                return

            # 上次刷过哪个任务，这次自动帮你选中；班级任务里绝不自动选中「未开始」的那条
            saved_id = str(((self.config.get("runtime") or {}).get("last_task_id")) or "")
            pick = 0
            if not is_study:
                pick = next((i for i, item in enumerate(self.tasks) if not self._is_unstarted(item)), 0)
            for index, item in enumerate(self.tasks):
                if saved_id and str(item.get("task_id")) == saved_id and (is_study or not self._is_unstarted(item)):
                    pick = index
                    self.log(f"🎯 已自动选回上次的任务：{item.get('task_name')}")
                    break
            if not is_study and self._is_unstarted(self.tasks[pick]):
                self.log("⚠️ 列表里的任务都还没开始过 —— 请先在词达人（微信/App）里点开其中一条，再回来「刷新任务」")
            self.ui(self.task_list.selection_clear, 0, "end")
            self.ui(self.task_list.selection_set, pick)
            self.ui(self.task_list.see, pick)

        self._run_in_thread(task, busy=False)

    @staticmethod
    def _is_unstarted(item: dict) -> bool:
        """判断任务是否「还没开始过」（词达人给未开始的任务发 task_id<=0）。"""
        try:
            return int(item.get("task_id") or 0) <= 0
        except (TypeError, ValueError):
            return True

    # ── 刷题 ──
    def on_start(self) -> None:
        if not self.tasks:
            messagebox.showinfo(APP_TITLE, "先点「刷新任务」并选中要刷的那一条")
            return
        hint = token_expiry_hint(self.config)
        if hint:
            self.log(hint)
        selection = self.task_list.curselection()
        index = int(selection[0]) if selection else 0
        self._run_in_thread(lambda: self._do_run(index))

    def on_stop(self) -> None:
        self.cancel.set()
        self.log("⏹️ 收到停止指令，正在收尾…")

    def _do_run(self, index: int) -> None:
        config = self.collect_config()
        client = self._client()
        chosen = self.tasks[min(index, len(self.tasks) - 1)]
        kind = self.var_kind.get()
        rounds = max(1, int(self.var_loop.get() or 1))
        max_score = float(self.var_max_score.get() or 10)
        bank_file = default_bank_path()
        bank = Bank(bank_file)

        # 记下这次刷的任务，下次打开自动选中
        config.setdefault("runtime", {})["last_task_id"] = str(chosen.get("task_id") or "")
        save_settings(config)

        reader = load_reader_from_config(config, logger=self.log) if self.var_ocr.get() else None
        solver = Solver(
            client,
            bank,
            kind=kind,
            llm=load_llm_from_config(config, logger=self.log),
            reader=reader,
            probe=bool(self.var_probe.get()),
            pace=(float(self.var_pace_lo.get() or 0), float(self.var_pace_hi.get() or 0)),
            cancel=self.cancel,
            logger=self.log,
        )

        for round_index in range(rounds):
            if self.cancel.is_set():
                break
            if rounds > 1:
                self.log(f"\n===== 第 {round_index + 1}/{rounds} 轮 =====")
            stats = self._run_once(client, solver, chosen, kind, config, max_score, bank)
            if stats is None or stats.cancelled:
                break
            self.ui(self.progress.configure, value=100)
        self.ui(self._refresh_status)
        self.log("✅ 结束")

    def _run_once(
        self,
        client: VocabgoClient,
        solver: Solver,
        chosen: dict[str, Any],
        kind: str,
        config: dict[str, Any],
        max_score: float,
        bank: Bank,
    ):
        if kind == "study":
            course_id = str(chosen.get("course_id") or self.var_course.get() or "CET4_v2")
            list_id = chosen.get("list_id")
            task_id = chosen.get("task_id")
            task_type = int(chosen.get("task_type") or 3)
            grade = int(chosen.get("grade") or (config.get("study") or {}).get("grade") or 2)
            self.log(f"→ 自学任务：{chosen.get('task_name')}（list_id={list_id}）")

            # 自学任务的 task_id=-1 是正常的：表示这条还没创建过。
            # 正路：Info(task_id=-1) 会让服务端把任务建出来并返回真实 id（见 client.ensure_study_task）。
            if not task_id or str(task_id) in ("-1", "0"):
                self.log("🆕 这条自学任务还没创建过，正在创建…")
                try:
                    task_id = client.ensure_study_task(course_id, list_id, task_type=task_type, grade=grade)
                except Exception as exc:
                    self.log(f"❌ {exc}")
                    self.ui(self.progress.configure, value=0)
                    return None
                self.log(f"🆕 已就绪，task_id={task_id}")

            words = (
                client.study_chose_word_list(task_id, course_id, list_id, task_type=task_type, grade=grade).get("data") or {}
            ).get("word_list") or []
            word_map = select_words(words, max_score=max_score)
            if word_map:
                self.log(f"📝 共 {len(words)} 词，待练 {sum(len(v) for v in word_map.values())} 词")
                client.study_submit_chose_word(task_id, course_id, list_id, word_map, task_type=task_type, grade=grade)
            solver.kind = "study"
            return solver.run(task_id=task_id, course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)

        task_id = int(chosen["task_id"])
        release_id = int(chosen["release_id"])
        if task_id <= 0:
            # 词达人给「还没开始过」的任务发 task_id=-1，直接请求会得到
            # 「对不起，加载任务信息失败，请返回首页重新加载」——这里提前拦住并说人话。
            self.log(f"❌ 「{chosen.get('task_name')}」还没开始过（编号={task_id}），服务端不认这个编号。")
            self.log("   解决：先在词达人（微信/App）里点开这条任务，让它变成「进行中」，再回来「刷新任务」。")
            self.log("   或者：换一条有正常编号的任务（列表里带 ⚠️未开始 的都跳过）。")
            self.ui(self.progress.configure, value=0)
            return None
        self.log(f"→ 班级任务：{chosen.get('task_name')}（task_id={task_id}）")
        words = (client.chose_word_list(task_id).get("data") or {}).get("word_list") or []
        word_map = select_words(words, max_score=max_score)
        if word_map:
            self.log(f"📝 共 {len(words)} 词，待练 {sum(len(v) for v in word_map.values())} 词")
            client.submit_chose_word(task_id, word_map)
        solver.kind = "class"
        stats = solver.run(task_id=task_id, release_id=release_id)
        try:
            signin = client.signin().get("data") or {}
            if signin:
                self.log(f"🏆 签到：累计 {signin.get('sign_in_total')} 天，积分 +{signin.get('integral')}")
        except Exception as exc:
            self.log(f"（签到跳过：{exc}）")
        self.log(f"🏦 题库已累计 {len(bank)} 条")
        return stats

    # ── 模型列表 ──
    def on_fetch_models(self) -> None:
        def task() -> None:
            helper = self._current_helper()
            if not helper.enabled:
                self.log("❌ 拉取模型失败：先把接口密钥填上（接口地址与模型名可以自动带出来）")
                return
            self.log(f"📋 正在拉取模型列表：{helper.endpoint_models()}")
            ids = helper.list_models()
            if not ids:
                self.log("❌ 没拉到模型列表：检查密钥 / 网络，或该服务商不提供模型列表接口")
                return
            text_model, vision_model = guess_models(ids, helper.provider)
            self.ui(self._apply_models, ids, text_model, vision_model)

        self._run_in_thread(task, busy=False)

    def _auto_fetch_models(self) -> None:
        """密钥填好后自动拉一次（没拉过、且当前没有别的任务在跑时才拉）。"""
        if self._models_loaded or not self.var_llm_key.get().strip():
            return
        if self.worker and self.worker.is_alive():
            return
        self.log("🔎 检测到接口密钥，自动拉取模型列表…")
        self.on_fetch_models()

    def _apply_models(self, ids: list[str], text_model: str, vision_model: str) -> None:
        self.cbo_llm_model.configure(values=ids)
        self.cbo_vision_model.configure(values=ids)
        if text_model:
            self.var_llm_model.set(text_model)
        if vision_model:
            self.var_vision_model.set(vision_model)
        self._models_loaded = True
        shown = ", ".join(ids[:12]) + (" …" if len(ids) > 12 else "")
        self.lbl_model_hint.configure(text=f"已拉取 {len(ids)} 个模型，已自动选中答题 / 读图模型")
        self.log(f"📋 可用模型 {len(ids)} 个：{shown}")
        self.log(f"✅ 自动选用 → 答题：{text_model or '未定'}　读图：{vision_model or '未定'}")

    # ── 体检 ──
    def on_llm_check(self) -> None:
        def task() -> None:
            config = self.collect_config()
            helper = load_llm_from_config(config, logger=self.log)
            self.log(f"🧠 大模型体检：{provider_label(helper.provider)}　接口：{helper.endpoint_chat() or '未配置'}")
            if not helper.enabled:
                self.log("❌ 缺接口地址 / 密钥 / 模型 —— 选好服务商后把接口密钥填上即可")
                return
            models = helper.list_models()
            if models:
                self.log(f"📋 可用模型：{', '.join(models[:10])}{' …' if len(models) > 10 else ''}")
            reply = helper._chat("Reply with exactly: OK", max_tokens=8)  # noqa: SLF001
            self.log(f"💬 对话测试：{'✅ ' + reply if reply else '❌ 失败，检查 key / 余额 / 网络'}")
            if self.var_ocr.get():
                report = probe_backend(load_reader_from_config(config, logger=self.log), "abandon")
                if report.get("ok"):
                    self.log(f"🖼️ 读图测试：✅ {report['text']!r}")
                elif report.get("ok") is None:
                    self.log(f"🖼️ 读图测试：⚠️ {report.get('reason')}")
                else:
                    self.log(f"🖼️ 读图测试：❌ {report.get('reason')}")

        self._run_in_thread(task, busy=False)

    def on_vision_check(self) -> None:
        def task() -> None:
            config = self.collect_config()
            reader = load_reader_from_config(config, logger=self.log)
            self.log(f"📷 读图体检：{backend_label(reader.backend)}")
            report = probe_backend(reader, "abandon")
            if report.get("ok"):
                self.log(f"✅ 识别成功：{report['text']!r}")
            elif report.get("ok") is None:
                self.log(f"⚠️ 跳过：{report.get('reason')}")
            else:
                self.log(f"❌ 失败：{report.get('reason')}（实际 {report.get('text')!r}）")

        self._run_in_thread(task, busy=False)


def main(argv: list[str] | None = None) -> int:
    # 先声明 DPI 感知，再按真实 DPI 设缩放 —— 否则高分屏要么糊、要么布局被撑破
    scale = 1.0
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
        probe = tk.Tk()
        scale = probe.winfo_fpixels("1i") / 72.0
        probe.destroy()
    except Exception:
        scale = 1.0

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", scale)
    except Exception:
        pass
    VocabgoApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
