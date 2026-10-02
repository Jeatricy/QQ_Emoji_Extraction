"""QQ 表情包提取与跨文件夹去重工具。Python 3.10+。

界面使用 CustomTkinter；浅色 / 深色两套配色可在界面右上角实时切换。
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import warnings
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    from PIL import Image, ImageOps
    from send2trash import send2trash as _send2trash
except ImportError as missing:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "缺少依赖",
        f"缺少 {missing.name}。请先双击“启动.bat”，或在脚本目录执行：\n\n"
        "python -m pip install -r requirements.txt\n\n"
        "安装完成后，再运行 qq_emoji_tool.py。",
        parent=_root,
    )
    _root.destroy()
    raise SystemExit(1)

try:
    import customtkinter as ctk
except ImportError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "缺少依赖",
        "缺少 customtkinter。请先双击“启动.bat”，或在脚本目录执行：\n\n"
        "python -m pip install -r requirements.txt\n\n"
        "安装完成后，再运行 qq_emoji_tool.py。",
        parent=_root,
    )
    _root.destroy()
    raise SystemExit(1)

from emoji_core import (
    Account,
    Cancelled,
    FileRecord,
    OperationResult,
    ScanResult,
    default_search_roots,
    delete_duplicates,
    detect_accounts,
    export_emojis,
    find_duplicates,
    overlaps,
    readable_directory,
)

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "qq_emoji_settings.json"
LOG_FILE = BASE_DIR / "qq_emoji_tool.log"
PAGE_SIZE = 24
THUMB_SIZE = (176, 112)
PREVIEW_SIZE = (700, 310)
APPEARANCE_MODES = ("浅色", "深色")
APPEARANCE_KEYS = {"浅色": "light", "深色": "dark"}

# 每个键都是 (浅色, 深色) 二元组，CustomTkinter 会按当前主题自动取用。
PALETTE = {
    "bg": ("#eef1f7", "#0f141c"),
    "surface": ("#ffffff", "#171e2a"),
    "raised": ("#f7f9fd", "#1e2735"),
    "hover": ("#e7ecf7", "#28323f"),
    "pressed": ("#dde4f3", "#303c4c"),
    "border": ("#dbe2ef", "#28323f"),
    "ink": ("#16203a", "#e9eef7"),
    "ink_soft": ("#41506b", "#b9c4d6"),
    "muted": ("#69768e", "#8b98ae"),
    "accent": ("#3b62f6", "#5b8cff"),
    "accent_hover": ("#2f52d8", "#7ba4ff"),
    "on_accent": ("#ffffff", "#0b1120"),
    "accent_soft": ("#e6ecff", "#22304d"),
    "danger": ("#dc3160", "#f2708f"),
    "danger_hover": ("#c02a52", "#ff8ba6"),
    "danger_soft": ("#fdeaf0", "#3a2130"),
    "track": ("#dfe5f2", "#242d3b"),
    "placeholder": ("#eef2f9", "#1b2431"),
    "thumb_bg": "#eef2f8",
}
FONT_FAMILY = "Microsoft YaHei UI"
DISABLED_FILL = ("#c9d2e4", "#2b3442")
DISABLED_TEXT = ("#8b96ab", "#5d6980")
warnings.simplefilter("error", Image.DecompressionBombWarning)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def read_settings() -> dict:
    try:
        content = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


def thumbnail(path: Path, size: tuple[int, int]) -> Image.Image:
    # 根据内容识别格式，不依赖 .jpg / .gif 后缀；动图使用首帧。
    with Image.open(path) as source:
        source.seek(0)
        source.thumbnail(size, Image.Resampling.LANCZOS)
        frame = ImageOps.exif_transpose(source).convert("RGBA")
        frame.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", size, PALETTE["thumb_bg"])
    canvas.alpha_composite(frame, ((size[0] - frame.width) // 2, (size[1] - frame.height) // 2))
    frame.close()
    return canvas.convert("RGB")


class EmojiApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("QQ 表情包 · 提取与去重")
        self.geometry("1180x940")
        self.minsize(1040, 820)
        self.settings = read_settings()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()
        self.close_pending = False
        self.closed = False
        self.disabled_widgets: list[tuple[tk.Widget, dict]] = []
        self.accounts: dict[str, Account] = {}
        self.selected_account: Account | None = None
        self.account_rows: dict[str, ctk.CTkFrame] = {}
        self.scan_result: ScanResult | None = None
        self.checked: set[Path] = set()
        self.visible_checks: dict[Path, tk.BooleanVar] = {}
        self.page = 0
        self.thumbnail_generation = 0
        self.thumbnail_cancel = threading.Event()
        self.thumbnail_labels: dict[Path, ctk.CTkLabel] = {}
        self.thumbnail_photos: list[ctk.CTkImage] = []
        self.messages: list[str] = []
        self.job_callback = None
        self._build_fonts()
        self._build_window()
        self._apply_appearance(self._saved_text("appearance", "浅色"))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(70, self._poll_events)
        self.after(200, self._detect)

    # ------------------------------------------------------------------ 外观

    def _build_fonts(self) -> None:
        self.fonts = {
            "title": ctk.CTkFont(family=FONT_FAMILY, size=25, weight="bold"),
            "section": ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
            "body": ctk.CTkFont(family=FONT_FAMILY, size=13),
            "body_bold": ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            "small": ctk.CTkFont(family=FONT_FAMILY, size=12),
            "tiny": ctk.CTkFont(family=FONT_FAMILY, size=11),
            "mono": ctk.CTkFont(family="Consolas", size=12),
        }

    def _paint_root(self) -> None:
        """切主题时同步主窗口及各页背景色。"""
        self.configure(fg_color=PALETTE["bg"])
        self.notebook.configure(fg_color=PALETTE["bg"])
        for tab in (self.export_tab, self.compare_tab):
            tab.configure(fg_color=PALETTE["bg"])

    def _apply_appearance(self, label: str, persist: bool = False) -> None:
        label = label if label in APPEARANCE_KEYS else APPEARANCE_MODES[0]
        ctk.set_appearance_mode(APPEARANCE_KEYS[label])
        self.appearance_var.set(label)
        self._paint_root()
        if persist:
            self._save_settings()
            self._log(f"界面配色已切换为{label}主题。")

    def _build_window(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(20, 12))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="QQ 表情包", font=self.fonts["title"], text_color=PALETTE["ink"]).pack(side="left")
        ctk.CTkLabel(
            titles, text="本地提取  /  内容去重", font=self.fonts["body"], text_color=PALETTE["muted"],
        ).pack(side="left", padx=(16, 0), pady=(8, 0))

        self.appearance_var = tk.StringVar(value=APPEARANCE_MODES[0])
        self.appearance_switch = ctk.CTkSegmentedButton(
            header, values=list(APPEARANCE_MODES), variable=self.appearance_var, font=self.fonts["small"],
            height=32, corner_radius=9, border_width=3,
            fg_color=PALETTE["surface"], selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hover"], unselected_color=PALETTE["surface"],
            unselected_hover_color=PALETTE["hover"], text_color=PALETTE["ink"],
            command=lambda value: self._apply_appearance(value, persist=True),
        )
        self.appearance_switch.pack(side="right", padx=(10, 0))
        self.log_button = ctk.CTkButton(
            header, text="操作记录", font=self.fonts["body"], command=self._show_log,
            width=104, height=32, corner_radius=9, fg_color=PALETTE["surface"], hover_color=PALETTE["hover"],
            text_color=PALETTE["ink"], border_width=1, border_color=PALETTE["border"],
        )
        self.log_button.pack(side="right")

        self.notebook = ctk.CTkTabview(
            self, fg_color=PALETTE["bg"], segmented_button_fg_color=PALETTE["surface"],
            segmented_button_selected_color=PALETTE["accent"],
            segmented_button_selected_hover_color=PALETTE["accent_hover"],
            segmented_button_unselected_color=PALETTE["surface"],
            segmented_button_unselected_hover_color=PALETTE["hover"],
            text_color=PALETTE["ink"], corner_radius=14, border_width=1, border_color=PALETTE["border"],
        )
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=26, pady=(0, 12))
        self.export_tab = self.notebook.add("01   提取表情包")
        self.compare_tab = self.notebook.add("02   查找重复文件")
        self._build_export_tab()
        self._build_compare_tab()

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 18))
        self.status = tk.StringVar(value="就绪")
        ctk.CTkLabel(
            footer, textvariable=self.status, font=self.fonts["small"], text_color=PALETTE["muted"],
            anchor="w", justify="left", wraplength=760,
        ).pack(side="left", fill="x", expand=True)
        self.cancel_button = ctk.CTkButton(
            footer, text="取消任务", font=self.fonts["body"], command=self._cancel, state="disabled",
            width=104, height=34, corner_radius=9, fg_color=PALETTE["surface"], hover_color=PALETTE["hover"],
            text_color=PALETTE["ink"], border_width=1, border_color=PALETTE["border"],
        )
        self.cancel_button.pack(side="right", padx=(14, 0))
        self.progress = ctk.CTkProgressBar(
            footer, width=230, height=8, corner_radius=4, fg_color=PALETTE["track"],
            progress_color=PALETTE["accent"],
        )
        self.progress.pack(side="right", pady=(10, 0))
        self.progress.set(0)

    # -------------------------------------------------------------- 提取页

    def _build_export_tab(self) -> None:
        tab = self.export_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        ctk.CTkLabel(head, text="选择保存过表情的 QQ 账号", font=self.fonts["section"],
                     text_color=PALETTE["ink"]).pack(anchor="w")
        ctk.CTkLabel(
            head, text="启动时自动查找“文档 / Tencent Files”。如果 QQ 数据放在其他位置，可以手动选择。",
            font=self.fonts["small"], text_color=PALETTE["muted"], anchor="w", justify="left",
        ).pack(anchor="w", pady=(5, 0))

        picker = ctk.CTkFrame(tab, fg_color="transparent")
        picker.grid(row=1, column=0, sticky="ew", padx=18)
        self.search_root = tk.StringVar(value=self._saved_text("search_root", str(default_search_roots()[0])))
        ctk.CTkEntry(
            picker, textvariable=self.search_root, font=self.fonts["small"], height=34, corner_radius=9,
            fg_color=PALETTE["surface"], border_color=PALETTE["border"], text_color=PALETTE["ink"],
        ).pack(side="left", fill="x", expand=True)
        self._make_button(picker, "选择数据目录", self._choose_search_root).pack(side="left", padx=(8, 0))
        self._make_button(picker, "重新检测", self._detect).pack(side="left", padx=(8, 0))

        self.account_list = ctk.CTkScrollableFrame(
            tab, fg_color=PALETTE["surface"], border_width=1, border_color=PALETTE["border"],
            corner_radius=12, scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["muted"],
        )
        self.account_list.grid(row=2, column=0, sticky="nsew", padx=18, pady=(12, 10))
        self.account_list.grid_columnconfigure(0, weight=1)
        self.account_hint = tk.StringVar(value="正在准备检测…")
        ctk.CTkLabel(
            tab, textvariable=self.account_hint, font=self.fonts["small"], text_color=PALETTE["muted"],
            anchor="w", justify="left", wraplength=1020,
        ).grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 4))

        details = ctk.CTkFrame(tab, fg_color="transparent")
        details.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 16))
        self._build_export_actions(details)

    def _build_export_actions(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        note = ctk.CTkFrame(parent, fg_color=PALETTE["raised"], corner_radius=12, border_width=1,
                            border_color=PALETTE["border"])
        note.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(note, text="原名保留，只改扩展名", font=self.fonts["body_bold"],
                     text_color=PALETTE["ink"], anchor="w").pack(anchor="w", padx=16, pady=(12, 4))
        ctk.CTkLabel(
            note,
            text="例如 abc123.jpg → abc123.gif。复制原始内容，不进行图片格式转换。\n"
                 "同名文件自动跳过；如果库中存在子文件夹，会保留子文件夹结构。",
            font=self.fonts["small"], text_color=PALETTE["muted"], anchor="w", justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(parent, text="选择导出位置", font=self.fonts["section"],
                     text_color=PALETTE["ink"], anchor="w").grid(row=1, column=0, sticky="ew", pady=(16, 8))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew")
        self.output_folder = tk.StringVar(value=self._saved_text("output_folder", str(BASE_DIR / "导出的表情包")))
        ctk.CTkEntry(
            row, textvariable=self.output_folder, font=self.fonts["small"], height=34, corner_radius=9,
            fg_color=PALETTE["surface"], border_color=PALETTE["border"], text_color=PALETTE["ink"],
        ).pack(side="left", fill="x", expand=True)
        self._make_button(row, "选择文件夹", lambda: self._choose_directory(self.output_folder)).pack(
            side="left", padx=(8, 0))

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        self.export_button = ctk.CTkButton(
            actions, text="开始提取", font=self.fonts["body_bold"], command=self._export,
            width=126, height=40, corner_radius=10, fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], text_color=PALETTE["on_accent"],
        )
        self.export_button.pack(side="left")
        self._make_button(actions, "打开导出文件夹",
                          lambda: self._open_folder(self.output_folder.get())).pack(side="left", padx=(10, 0))
        self._make_button(actions, "用导出目录去重 →", self._use_export_for_compare).pack(side="right")

    def _render_accounts(self, accounts: list[Account], selected_folder: str) -> None:
        for row in self.account_rows.values():
            row.destroy()
        self.account_rows.clear()
        self.accounts.clear()
        for index, account in enumerate(accounts):
            iid = str(index)
            self.accounts[iid] = account
            row = ctk.CTkFrame(self.account_list, fg_color="transparent", corner_radius=10, cursor="hand2")
            row.grid(row=index, column=0, sticky="ew", padx=8, pady=2)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=account.qq, font=self.fonts["body_bold"], text_color=PALETTE["ink"],
                         width=140, anchor="w").grid(row=0, column=0, sticky="w", padx=(12, 0), pady=9)
            ctk.CTkLabel(row, text=f"{account.count} 个文件", font=self.fonts["small"],
                         text_color=PALETTE["ink_soft"], width=96, anchor="w").grid(row=0, column=1, sticky="w")
            ctk.CTkLabel(row, text=str(account.folder), font=self.fonts["tiny"], text_color=PALETTE["muted"],
                         anchor="w").grid(row=0, column=2, sticky="ew", padx=(0, 12))
            for widget in (row, *row.winfo_children()):
                widget.bind("<Button-1>", lambda _event, key=iid: self._select_account(key))
            self.account_rows[iid] = row
        if accounts:
            self._select_account(selected_folder or "0")

    def _select_account(self, iid: str) -> None:
        if iid not in self.accounts:
            return
        for key, row in self.account_rows.items():
            row.configure(fg_color=PALETTE["accent_soft"] if key == iid else "transparent")
        self.selected_account = self.accounts[iid]
        account = self.selected_account
        self.account_hint.set(f"已选择 QQ {account.qq} · {account.count} 个文件\n{account.folder}")

    # -------------------------------------------------------------- 去重页

    def _build_compare_tab(self) -> None:
        tab = self.compare_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(7, weight=1)

        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        ctk.CTkLabel(head, text="比较两个文件夹的文件内容", font=self.fonts["section"],
                     text_color=PALETTE["ink"]).pack(anchor="w")
        ctk.CTkLabel(
            head, text="逐个文件计算 SHA-256，文件名不同也能找到相同内容。只列出两个文件夹之间的重复文件。",
            font=self.fonts["small"], text_color=PALETTE["muted"], anchor="w", justify="left",
        ).pack(anchor="w", pady=(5, 0))

        self.folder_a = tk.StringVar(value=self._saved_text("folder_a", ""))
        self.folder_b = tk.StringVar(value=self._saved_text("folder_b", ""))
        for offset, (side, variable) in enumerate((("A", self.folder_a), ("B", self.folder_b))):
            row = ctk.CTkFrame(tab, fg_color="transparent")
            row.grid(row=1 + offset, column=0, sticky="ew", padx=18, pady=3)
            ctk.CTkLabel(row, text=f"文件夹 {side}", font=self.fonts["body"], text_color=PALETTE["ink_soft"],
                         width=72, anchor="w").pack(side="left")
            ctk.CTkEntry(
                row, textvariable=variable, font=self.fonts["small"], height=34, corner_radius=9,
                fg_color=PALETTE["surface"], border_color=PALETTE["border"], text_color=PALETTE["ink"],
            ).pack(side="left", fill="x", expand=True)
            self._make_button(row, "选择文件夹", lambda v=variable: self._choose_directory(v)).pack(
                side="left", padx=(8, 0))

        options = ctk.CTkFrame(tab, fg_color="transparent")
        options.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 10))
        self.recursive = tk.BooleanVar(value=bool(self.settings.get("recursive", True)))
        self.recursive_box = ctk.CTkCheckBox(
            options, text="包含子文件夹", variable=self.recursive, command=self._invalidate_results,
            font=self.fonts["body"], text_color=PALETTE["ink"], fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], border_color=PALETTE["muted"],
            checkbox_width=22, checkbox_height=22, corner_radius=6,
        )
        self.recursive_box.pack(side="left")
        self.scan_button = ctk.CTkButton(
            options, text="扫描重复文件", font=self.fonts["body_bold"], command=self._scan,
            width=132, height=38, corner_radius=10, fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], text_color=PALETTE["on_accent"],
        )
        self.scan_button.pack(side="right")

        summary = ctk.CTkFrame(tab, fg_color=PALETTE["surface"], corner_radius=12, border_width=1,
                               border_color=PALETTE["border"])
        summary.grid(row=4, column=0, sticky="ew", padx=18)
        summary.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(summary, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        inner.grid_columnconfigure(0, weight=1)
        self.summary_text = tk.StringVar(value="选择两个文件夹后开始扫描")
        ctk.CTkLabel(inner, textvariable=self.summary_text, font=self.fonts["body_bold"],
                     text_color=PALETTE["ink"], anchor="w", justify="left").grid(row=0, column=0, sticky="ew")
        self.selection_text = tk.StringVar(value="已勾选 0 个文件")
        ctk.CTkLabel(inner, textvariable=self.selection_text, font=self.fonts["small"],
                     text_color=PALETTE["accent"]).grid(row=0, column=1, sticky="e")
        target = ctk.CTkFrame(inner, fg_color="transparent")
        target.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(target, text="清理位置：", font=self.fonts["small"],
                     text_color=PALETTE["ink_soft"]).pack(side="left")
        self.target_side = tk.StringVar(value="B")
        self.side_buttons: dict[str, ctk.CTkRadioButton] = {}
        for value, text in (("A", "文件夹 A（保留 B）"), ("B", "文件夹 B（保留 A）")):
            button = ctk.CTkRadioButton(
                target, text=text, value=value, variable=self.target_side, command=self._change_side,
                font=self.fonts["small"], text_color=PALETTE["ink"], fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hover"], border_color=PALETTE["muted"], radiobutton_width=20,
                radiobutton_height=20,
            )
            button.pack(side="left", padx=(6, 14))
            self.side_buttons[value] = button

        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.grid(row=5, column=0, sticky="ew", padx=18, pady=(10, 6))
        self._make_button(toolbar, "全选所有页", self._select_all, width=98).pack(side="left")
        self._make_button(toolbar, "取消全选", self._select_none, width=88).pack(side="left", padx=(6, 8))
        self.delete_button = ctk.CTkButton(
            toolbar, text="删除勾选项（回收站）", font=self.fonts["body"], command=self._delete, state="disabled",
            width=168, height=34, corner_radius=9, fg_color=PALETTE["danger"], hover_color=PALETTE["danger_hover"],
            text_color=PALETTE["on_accent"],
        )
        self.delete_button.pack(side="right")

        self.target_hint = tk.StringVar(value="")
        ctk.CTkLabel(tab, textvariable=self.target_hint, font=self.fonts["small"], text_color=PALETTE["muted"],
                     anchor="w", justify="left", wraplength=1000).grid(row=6, column=0, sticky="ew", padx=18)

        pager = ctk.CTkFrame(tab, fg_color="transparent")
        pager.grid(row=8, column=0, sticky="ew", padx=18, pady=(10, 14))
        self.previous_button = self._make_button(pager, "← 上一页", lambda: self._turn_page(-1), width=92)
        self.previous_button.pack(side="left")
        self.page_text = tk.StringVar(value="第 0 / 0 页")
        ctk.CTkLabel(pager, textvariable=self.page_text, font=self.fonts["small"],
                     text_color=PALETTE["ink_soft"], width=110).pack(side="left", padx=10)
        self.next_button = self._make_button(pager, "下一页 →", lambda: self._turn_page(1), width=92)
        self.next_button.pack(side="left")
        self.gallery_hint = tk.StringVar(value="扫描后显示重复文件 · 每页 24 个")
        ctk.CTkLabel(pager, textvariable=self.gallery_hint, font=self.fonts["small"],
                     text_color=PALETTE["muted"]).pack(side="right")

        self.gallery = ctk.CTkScrollableFrame(
            tab, fg_color=PALETTE["surface"], border_width=1, border_color=PALETTE["border"],
            corner_radius=12, scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["muted"],
        )
        self.gallery.grid(row=7, column=0, sticky="nsew", padx=18, pady=(6, 0))
        self.folder_a.trace_add("write", lambda *_: self._invalidate_results())
        self.folder_b.trace_add("write", lambda *_: self._invalidate_results())
        self._render_results()

    # ------------------------------------------------------------ 小工具

    def _make_button(self, parent, text: str, command, width: int = 0) -> ctk.CTkButton:
        button = ctk.CTkButton(
            parent, text=text, command=command, font=self.fonts["body"], height=34, corner_radius=9,
            fg_color=PALETTE["surface"], hover_color=PALETTE["hover"], text_color=PALETTE["ink"],
            border_width=1, border_color=PALETTE["border"],
        )
        if width:
            button.configure(width=width)
        return button

    def _set_enabled(self, widget, enabled: bool) -> None:
        """统一切换可用状态；可填色的控件在禁用时一并变灰，保证有明确反馈。"""
        fills = isinstance(widget, (ctk.CTkButton, ctk.CTkEntry))
        if not enabled:
            if fills and "_normal_fill" not in widget.__dict__:
                widget._normal_fill = widget.cget("fg_color")
                widget._normal_text = widget.cget("text_color")
                widget.configure(fg_color=DISABLED_FILL, text_color=DISABLED_TEXT)
            widget.configure(state="disabled")
        else:
            widget.configure(state="normal")
            if fills and "_normal_fill" in widget.__dict__:
                widget.configure(fg_color=widget._normal_fill, text_color=widget._normal_text)
                del widget.__dict__["_normal_fill"]
                del widget.__dict__["_normal_text"]

    def _saved_text(self, key: str, default: str) -> str:
        value = self.settings.get(key, default)
        return value if isinstance(value, str) else default

    def _save_settings(self) -> None:
        settings = {
            "search_root": self.search_root.get(),
            "output_folder": self.output_folder.get(),
            "folder_a": self.folder_a.get(),
            "folder_b": self.folder_b.get(),
            "recursive": self.recursive.get(),
            "appearance": self.appearance_var.get(),
            "last_source": (str(self.selected_account.folder) if self.selected_account
                            else self._saved_text("last_source", "")),
        }
        try:
            temporary = SETTINGS_FILE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(SETTINGS_FILE)
            self.settings = settings
        except OSError as exc:
            self._log(f"无法保存路径设置：{exc}")

    def _choose_directory(self, variable: tk.StringVar) -> bool:
        if self.busy:
            return False
        current = Path(variable.get().strip().strip('"')).expanduser() if variable.get().strip() else BASE_DIR
        initial = current if current.is_dir() else current.parent
        chosen = filedialog.askdirectory(parent=self, title="选择文件夹", initialdir=str(initial), mustexist=True)
        if chosen:
            variable.set(str(Path(chosen)))
            self._save_settings()
            return True
        return False

    def _choose_search_root(self) -> None:
        if self._choose_directory(self.search_root):
            self._detect()

    def _detect(self) -> None:
        if self.busy:
            return
        roots = default_search_roots()
        extra = self.search_root.get().strip().strip('"')
        if extra:
            roots.insert(0, Path(os.path.expandvars(extra)).expanduser().resolve())
        self.account_hint.set("正在检测本地表情包库…")
        self._start_job("检测 QQ 账号", lambda cancel, report: detect_accounts(roots, cancel, report), self._detected)

    def _detected(self, payload) -> None:
        accounts, errors = payload
        saved = self._saved_text("last_source", "")
        selected_iid = next((str(index) for index, account in enumerate(accounts)
                             if str(account.folder) == saved), "")
        self._render_accounts(accounts, selected_iid)
        if accounts:
            self.status.set(f"检测完成，找到 {len(accounts)} 个有本地表情的账号目录")
        else:
            self.selected_account = None
            self.account_hint.set("没有发现有文件的表情库。可选择其他 Tencent Files 目录、QQ 账号目录，"
                                  "或 personal_emoji 下的 Ori 目录。")
            self.status.set("未发现本地表情库，可手动选择数据目录")
        for error in errors:
            self._log(error)
        self._log(f"账号检测完成：找到 {len(accounts)} 个表情库。")

    # ------------------------------------------------------------ 提取流程

    def _export(self) -> None:
        if self.busy:
            return
        if self.selected_account is None:
            messagebox.showinfo("先选择账号", "请先检测并选择一个 QQ 账号。", parent=self)
            return
        try:
            source = readable_directory(str(self.selected_account.folder))
            text = self.output_folder.get().strip().strip('"')
            if not text:
                raise ValueError("请选择导出文件夹。")
            destination = Path(os.path.expandvars(text)).expanduser().resolve()
            if destination.exists() and not destination.is_dir():
                raise ValueError("导出路径不是文件夹。")
            if overlaps(source, destination):
                raise ValueError("导出目录和源表情库不能相同，也不能互相包含。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法导出", str(exc), parent=self)
            return
        self._save_settings()
        self._log(f"开始提取：{source} → {destination}")
        self._start_job("提取表情包", lambda cancel, report: export_emojis(source, destination, cancel, report),
                        self._exported)

    def _exported(self, result: OperationResult) -> None:
        title = "提取已取消" if result.cancelled else "提取完成"
        text = f"已导出 {len(result.completed)} 个，跳过同名文件 {len(result.skipped)} 个，异常 {len(result.errors)} 条。"
        self._operation_log(title, result)
        self.status.set(text)
        if not self.close_pending:
            messagebox.showinfo(title, text + "\n\n已导出的文件保留在所选目录。跳过或失败的详情可在“操作记录”中查看。",
                                parent=self)

    def _use_export_for_compare(self) -> None:
        if self.busy:
            return
        self.folder_b.set(self.output_folder.get())
        self.notebook.set("02   查找重复文件")
        self._save_settings()

    # ------------------------------------------------------------ 扫描流程

    def _scan(self) -> None:
        if self.busy:
            return
        try:
            first = readable_directory(self.folder_a.get())
            second = readable_directory(self.folder_b.get())
            if overlaps(first, second):
                raise ValueError("文件夹 A 和 B 不能是同一个目录，也不能互相包含。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法扫描", str(exc), parent=self)
            return
        self._invalidate_results()
        self._save_settings()
        recursive = self.recursive.get()
        self._log(f"开始比较：A = {first}；B = {second}；包含子文件夹 = {recursive}")
        self._start_job("扫描重复文件",
                        lambda cancel, report: find_duplicates(first, second, recursive, cancel, report),
                        self._scanned)

    def _scanned(self, result: ScanResult) -> None:
        for error in result.errors:
            self._log(error)
        if result.cancelled:
            self.summary_text.set("扫描已取消，请重新扫描以获取完整结果")
            self.status.set("扫描已取消")
            self._log("重复文件扫描已取消，没有展示部分结果。")
            return
        self.scan_result = result
        self.checked.clear()
        self.page = 0
        self._update_summary()
        self._render_results()
        count = sum(len(records) for records in result.files.values())
        text = f"扫描完成：已计算 {count} 个文件，找到 {len(result.groups)} 组重复内容"
        if result.errors:
            text += f"；{len(result.errors)} 条异常详见操作记录"
        self.status.set(text)
        self._log(text)

    def _invalidate_results(self) -> None:
        if self.busy:
            return
        self.scan_result = None
        self.checked.clear()
        self.page = 0
        self.summary_text.set("选择两个文件夹后开始扫描")
        self._render_results()

    def _update_summary(self) -> None:
        if self.scan_result is None:
            return
        groups = self.scan_result.groups
        a_count = sum(len(group["A"]) for group in groups.values())
        b_count = sum(len(group["B"]) for group in groups.values())
        self.summary_text.set(f"{len(groups)} 组相同内容   ·   A 中 {a_count} 个重复文件   ·   "
                              f"B 中 {b_count} 个重复文件")

    def _target_records(self) -> list[FileRecord]:
        if self.scan_result is None:
            return []
        side = self.target_side.get()
        return sorted(
            (record for group in self.scan_result.groups.values() for record in group[side]),
            key=lambda record: (record.digest, record.relative.casefold()),
        )

    def _change_side(self) -> None:
        if self.busy:
            return
        self.checked.clear()
        self.page = 0
        self._render_results()

    # ------------------------------------------------------------ 结果画廊

    def _render_results(self) -> None:
        self.thumbnail_cancel.set()
        self.thumbnail_cancel = threading.Event()
        self.thumbnail_generation += 1
        generation = self.thumbnail_generation
        for child in self.gallery.winfo_children():
            child.destroy()
        self.visible_checks.clear()
        self.thumbnail_labels.clear()
        self.thumbnail_photos.clear()
        records = self._target_records()
        page_count = math.ceil(len(records) / PAGE_SIZE)
        self.page = min(self.page, max(0, page_count - 1))
        self.page_text.set(f"第 {self.page + 1 if records else 0} / {page_count} 页")
        self._set_enabled(self.previous_button, self.page > 0 and not self.busy)
        self._set_enabled(self.next_button, self.page + 1 < page_count and not self.busy)
        side = self.target_side.get()
        other = "B" if side == "A" else "A"
        if self.scan_result:
            self.target_hint.set(f"当前展示并清理 {side}：{self.scan_result.roots[side]}")
        else:
            self.target_hint.set("扫描后选择要清理的一侧；另一侧保留。")
        self._update_selection()
        for column in range(4):
            self.gallery.grid_columnconfigure(column, weight=1, uniform="cards")
        if not records:
            message = ("两个文件夹之间没有发现内容相同的文件。" if self.scan_result
                       else "扫描完成后，重复文件的缩略图会显示在这里。")
            ctk.CTkLabel(self.gallery, text=message, font=self.fonts["body"], text_color=PALETTE["muted"],
                         justify="center", wraplength=620).grid(row=0, column=0, columnspan=4, pady=70)
            return
        page_records = records[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        for index, record in enumerate(page_records):
            self._build_card(index, record, side, other)
        cancel = self.thumbnail_cancel

        def load_thumbnails() -> None:
            # 缩略图在线程中解码，避免大图阻塞界面；控件与图片对象只在主线程创建。
            for record in page_records:
                if cancel.is_set():
                    return
                try:
                    image = thumbnail(record.path, THUMB_SIZE)
                except Exception:
                    image = None
                if cancel.is_set():
                    if image is not None:
                        image.close()
                    return
                self.events.put(("thumbnail", (generation, record.path, image)))

        if page_records:
            threading.Thread(target=load_thumbnails, daemon=True, name="emoji-thumbnails").start()

    def _build_card(self, index: int, record: FileRecord, side: str, other: str) -> None:
        card = ctk.CTkFrame(self.gallery, fg_color=PALETTE["raised"], corner_radius=12, border_width=1,
                            border_color=PALETTE["border"])
        card.grid(row=index // 4, column=index % 4, sticky="nsew", padx=6, pady=6)
        card.grid_columnconfigure(0, weight=1)
        variable = tk.BooleanVar(value=record.path in self.checked)
        self.visible_checks[record.path] = variable
        ctk.CTkCheckBox(
            card, text=f"{side} · 勾选此文件", variable=variable,
            command=lambda r=record, v=variable: self._toggle_record(r, v.get()),
            font=self.fonts["small"], text_color=PALETTE["ink"], fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], border_color=PALETTE["muted"],
            checkbox_width=20, checkbox_height=20, corner_radius=6,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        label = ctk.CTkLabel(
            card, text="正在加载缩略图…", font=self.fonts["small"], width=THUMB_SIZE[0], height=THUMB_SIZE[1],
            corner_radius=8, fg_color=PALETTE["placeholder"], text_color=PALETTE["muted"],
        )
        label.grid(row=1, column=0, padx=12, pady=4)
        label.bind("<Button-1>", lambda _event, r=record: self._preview(r))
        label.configure(cursor="hand2")
        self.thumbnail_labels[record.path] = label
        ctk.CTkLabel(card, text=record.path.name, font=self.fonts["small"], text_color=PALETTE["ink"],
                     anchor="w", justify="left", wraplength=186).grid(row=2, column=0, sticky="ew", padx=12)
        ctk.CTkLabel(card, text=f"{format_size(record.size)}   ·   {other} 中有 "
                                f"{len(self.scan_result.groups[record.digest][other])} 份相同内容",
                     font=self.fonts["tiny"], text_color=PALETTE["muted"], anchor="w", justify="left",
                     wraplength=186).grid(row=3, column=0, sticky="ew", padx=12, pady=(5, 0))
        parent_text = str(Path(record.relative).parent)
        if parent_text != ".":
            if len(parent_text) > 42:
                parent_text = "…" + parent_text[-41:]
            ctk.CTkLabel(card, text=parent_text, font=self.fonts["tiny"], text_color=PALETTE["muted"],
                         anchor="w", justify="left", wraplength=186).grid(row=4, column=0, sticky="ew",
                                                                          padx=12, pady=(2, 11))
        else:
            card.grid_rowconfigure(4, minsize=11)

    def _toggle_record(self, record: FileRecord, checked: bool) -> None:
        if self.busy:
            return
        if checked:
            self.checked.add(record.path)
        else:
            self.checked.discard(record.path)
        self._update_selection()

    def _select_all(self) -> None:
        if self.busy:
            return
        self.checked = {record.path for record in self._target_records()}
        for variable in self.visible_checks.values():
            variable.set(True)
        self._update_selection()

    def _select_none(self) -> None:
        if self.busy:
            return
        self.checked.clear()
        for variable in self.visible_checks.values():
            variable.set(False)
        self._update_selection()

    def _update_selection(self) -> None:
        records = [record for record in self._target_records() if record.path in self.checked]
        total = sum(record.size for record in records)
        self.selection_text.set(f"已勾选 {len(records)} 个 · {format_size(total)}")
        self._set_enabled(self.delete_button, bool(records) and not self.busy)

    def _turn_page(self, delta: int) -> None:
        if self.busy:
            return
        self.page = max(0, self.page + delta)
        self._render_results()

    # ------------------------------------------------------------ 预览窗口

    def _preview(self, record: FileRecord) -> None:
        if self.busy or self.scan_result is None:
            return
        window = ctk.CTkToplevel(self)
        window.title(f"重复文件预览 · {record.path.name}")
        window.geometry("820x760")
        window.minsize(700, 640)
        window.configure(fg_color=PALETTE["bg"])
        body = ctk.CTkFrame(window, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(body, text=record.path.name, font=self.fonts["section"], text_color=PALETTE["ink"],
                     anchor="w", justify="left", wraplength=760).pack(anchor="w")
        ctk.CTkLabel(body, text=str(record.path), font=self.fonts["small"], text_color=PALETTE["muted"],
                     anchor="w", justify="left", wraplength=760).pack(anchor="w", pady=(5, 12))
        try:
            frame = thumbnail(record.path, PREVIEW_SIZE)
            photo = ctk.CTkImage(light_image=frame, dark_image=frame, size=PREVIEW_SIZE)
            image_label = ctk.CTkLabel(body, text="", image=photo, corner_radius=10)
            image_label.image = photo
            image_label.pack(pady=(0, 6))
        except Exception as exc:
            ctk.CTkLabel(body, text=f"此文件无法显示图片预览\n{exc}", font=self.fonts["small"],
                         text_color=PALETTE["muted"], justify="center", wraplength=700).pack(pady=40)
        ctk.CTkLabel(body, text=f"{format_size(record.size)} · 图片及动图均预览首帧", font=self.fonts["small"],
                     text_color=PALETTE["muted"], anchor="w").pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(body, text="SHA-256", font=self.fonts["body_bold"], text_color=PALETTE["ink"],
                     anchor="w").pack(anchor="w", pady=(14, 5))
        digest = ctk.CTkEntry(body, font=self.fonts["mono"], height=34, corner_radius=9,
                              fg_color=PALETTE["surface"], border_color=PALETTE["border"],
                              text_color=PALETTE["ink_soft"])
        digest.insert(0, record.digest)
        digest.configure(state="readonly")
        digest.pack(fill="x")
        other = "B" if self.target_side.get() == "A" else "A"
        ctk.CTkLabel(body, text=f"文件夹 {other} 中内容相同的文件", font=self.fonts["body_bold"],
                     text_color=PALETTE["ink"], anchor="w").pack(anchor="w", pady=(16, 6))
        matches = ctk.CTkTextbox(body, height=110, corner_radius=10, fg_color=PALETTE["surface"],
                                 border_width=1, border_color=PALETTE["border"], text_color=PALETTE["ink_soft"],
                                 font=self.fonts["small"])
        matches.pack(fill="both", expand=True)
        for candidate in self.scan_result.groups[record.digest][other]:
            matches.insert("end", str(candidate.path) + "\n")
        matches.configure(state="disabled")
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(14, 0))
        self._make_button(buttons, "打开所在文件夹",
                          lambda: self._open_folder(str(record.path.parent))).pack(side="left")
        self._make_button(buttons, "复制 SHA-256",
                          lambda: self._copy_text(record.digest)).pack(side="left", padx=8)
        self._make_button(buttons, "关闭", window.destroy).pack(side="right")
        window.transient(self)

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    # ------------------------------------------------------------ 清理流程

    def _delete(self) -> None:
        if self.busy or self.scan_result is None:
            return
        selected = [record for record in self._target_records() if record.path in self.checked]
        if not selected:
            return
        side = self.target_side.get()
        other = "B" if side == "A" else "A"
        scan = self.scan_result
        size = format_size(sum(record.size for record in selected))
        confirmed = messagebox.askyesno(
            "确认移入回收站",
            f"将文件夹 {side} 中勾选的 {len(selected)} 个文件（{size}）移入回收站：\n"
            f"{scan.roots[side]}\n\n"
            f"保留文件夹 {other}：\n{scan.roots[other]}\n\n"
            "删除前会重新检查文件内容及另一侧副本。是否继续？",
            parent=self, icon="warning", default="no",
        )
        if not confirmed:
            return
        self.thumbnail_cancel.set()
        self._log(f"清理文件夹 {side}：用户确认将 {len(selected)} 个勾选文件移入回收站。")
        self._start_job("清理重复文件",
                        lambda cancel, report: delete_duplicates(scan, side, selected, cancel, report),
                        self._deleted)

    def _deleted(self, result: OperationResult) -> None:
        removed = set(result.completed)
        if self.scan_result:
            for side in ("A", "B"):
                self.scan_result.files[side] = [record for record in self.scan_result.files[side]
                                                if record.path not in removed]
            groups = {}
            for digest, group in self.scan_result.groups.items():
                updated = {side: [record for record in group[side] if record.path not in removed]
                           for side in ("A", "B")}
                if updated["A"] and updated["B"]:
                    groups[digest] = updated
            self.scan_result.groups = groups
        self.checked.clear()
        self._update_summary()
        self._render_results()
        title = "清理已停止" if result.cancelled else "清理完成"
        self._operation_log(title, result)
        for path in result.completed:
            self._log(f"已移入回收站：{path}")
        text = f"已移入回收站 {len(result.completed)} 个文件；异常 {len(result.errors)} 条。"
        if result.cancelled:
            text += "尚未处理的文件保留。"
        self.status.set(text)
        if not self.close_pending:
            messagebox.showinfo(title, text + "\n\n另一侧文件保持不变。详细结果见“操作记录”。", parent=self)

    # ------------------------------------------------------------ 任务与状态

    def _lock_controls(self) -> None:
        self.disabled_widgets = []

        def visit(parent) -> None:
            for child in parent.winfo_children():
                if isinstance(child, (ctk.CTkButton, ctk.CTkEntry, ctk.CTkCheckBox, ctk.CTkRadioButton,
                                      ctk.CTkSegmentedButton)) and child not in (self.cancel_button,
                                                                                self.appearance_switch):
                    self.disabled_widgets.append((child, {"state": child.cget("state")}))
                    self._set_enabled(child, False)
                visit(child)

        visit(self)
        self._set_enabled(self.cancel_button, True)

    def _unlock_controls(self) -> None:
        for widget, previous in self.disabled_widgets:
            if not widget.winfo_exists():
                continue
            self._set_enabled(widget, True)
            if previous.get("state") == "disabled":
                self._set_enabled(widget, False)
        self.disabled_widgets = []
        self._set_enabled(self.cancel_button, False)

    def _start_job(self, name: str, work, callback) -> None:
        if self.busy:
            return
        self.busy = True
        self.cancel_event = threading.Event()
        self.job_callback = callback
        self._lock_controls()
        self.status.set(name + "…")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        last_progress = 0.0

        def report(kind: str, payload) -> None:
            nonlocal last_progress
            if kind == "progress":
                done, total, _text = payload
                now = time.monotonic()
                if now - last_progress < 0.08 and not (total and done == total):
                    return
                last_progress = now
            self.events.put((kind, payload))

        def run() -> None:
            try:
                payload = work(self.cancel_event, report)
                self.events.put(("finished", (payload, None)))
            except Cancelled:
                self.events.put(("finished", (None, "cancelled")))
            except Exception as exc:
                logging.exception("后台任务失败：%s", name)
                self.events.put(("finished", (None, str(exc))))

        # 退出窗口时先请求取消，等待文件句柄关闭及不完整副本清理完成。
        threading.Thread(target=run, daemon=False, name="emoji-file-job").start()

    def _poll_events(self) -> None:
        try:
            for _ in range(120):
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    done, total, text = payload
                    self.status.set(text if len(text) <= 105 else text[:102] + "…")
                    if total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                        self.progress.set(min(1.0, max(0.0, done / total)))
                    elif self.progress.cget("mode") != "indeterminate":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start()
                elif kind == "thumbnail":
                    generation, path, image = payload
                    label = self.thumbnail_labels.get(path)
                    if generation == self.thumbnail_generation and label is not None and label.winfo_exists():
                        if image is None:
                            label.configure(text="无法预览\n仍可按内容去重", fg_color=PALETTE["placeholder"])
                        else:
                            # CTkImage 会在主线程按当前 DPI 缩放，缩略图因此保持清晰。
                            ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=THUMB_SIZE)
                            self.thumbnail_photos.append(ctk_image)
                            label.configure(image=ctk_image, text="")
                elif kind == "finished":
                    result, error = payload
                    self.busy = False
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    self._unlock_controls()
                    if error == "cancelled":
                        self.status.set("任务已取消")
                        self.account_hint.set("检测已取消，可重新检测。")
                    elif error:
                        self.status.set("任务失败，详情见操作记录")
                        self._log(error)
                        if not self.close_pending:
                            messagebox.showerror("任务失败", error, parent=self)
                    elif self.job_callback:
                        self.job_callback(result)
                    if self.close_pending:
                        self._finish_close()
                        return
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.after(70, self._poll_events)

    def _cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self._set_enabled(self.cancel_button, False)
            self.status.set("正在取消，等待当前文件操作结束…")

    # ------------------------------------------------------------ 记录与收尾

    def _operation_log(self, title: str, result: OperationResult) -> None:
        self._log(f"{title}：完成 {len(result.completed)}，跳过 {len(result.skipped)}，异常 {len(result.errors)}。")
        for line in result.skipped + result.errors:
            self._log(line)

    def _log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.messages.append(line)
        logging.info(text)

    def _show_log(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("操作记录")
        window.geometry("920x580")
        window.configure(fg_color=PALETTE["bg"])
        body = ctk.CTkFrame(window, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=16)
        ctk.CTkLabel(body, text="操作记录", font=self.fonts["section"], text_color=PALETTE["ink"],
                     anchor="w").pack(anchor="w", pady=(0, 10))
        text = ctk.CTkTextbox(body, corner_radius=10, fg_color=PALETTE["surface"], border_width=1,
                              border_color=PALETTE["border"], text_color=PALETTE["ink_soft"],
                              font=self.fonts["small"])
        text.pack(fill="both", expand=True)
        text.insert("end", "\n".join(self.messages) or "暂无操作记录。")
        text.configure(state="disabled")
        text.see("end")
        ctk.CTkLabel(body, text=f"日志文件：{LOG_FILE}", font=self.fonts["tiny"], text_color=PALETTE["muted"],
                     anchor="w", justify="left", wraplength=860).pack(anchor="w", pady=(10, 0))
        window.transient(self)

    def _open_folder(self, text: str) -> None:
        try:
            folder = readable_directory(text)
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=self)

    def _close(self) -> None:
        if self.busy:
            if messagebox.askyesno("任务仍在进行", "是否取消当前任务，并在当前文件处理结束后退出？", parent=self):
                self.close_pending = True
                self._cancel()
            return
        self._finish_close()

    def _finish_close(self) -> None:
        self.closed = True
        self.thumbnail_cancel.set()
        self.progress.stop()
        # 关闭时同时撤销启动检测及轮询，避免 Tk 销毁后仍执行定时回调。
        for after_id in self.tk.splitlist(self.tk.call("after", "info")):
            self.after_cancel(after_id)
        self._save_settings()
        self.destroy()

    def report_callback_exception(self, exc_type, exc_value, traceback) -> None:
        logging.error("界面操作失败", exc_info=(exc_type, exc_value, traceback))
        messagebox.showerror("界面操作失败", f"{exc_value}\n\n详细错误已写入日志文件。", parent=self)


def main() -> None:
    try:
        from logging.handlers import RotatingFileHandler

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")],
        )
    except OSError:
        logging.basicConfig(level=logging.INFO)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    # 先按上次选择设定配色，避免启动时闪一下另一种主题。
    ctk.set_default_color_theme("blue")
    saved = read_settings().get("appearance", APPEARANCE_MODES[0])
    ctk.set_appearance_mode(APPEARANCE_KEYS.get(saved, "light"))
    try:
        app = EmojiApp()
        app.mainloop()
    except Exception:
        logging.exception("程序启动失败")
        raise


if __name__ == "__main__":
    main()
