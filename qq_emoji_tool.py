"""QQ 表情包提取与多文件夹去重工具。Python 3.10+。

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
    dedup_candidates,
    default_keepers,
    default_search_roots,
    delete_duplicates,
    detect_accounts,
    export_emojis,
    find_duplicates,
    normalized,
    overlaps,
    prepare_folders,
    readable_directory,
)

BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "qq_emoji_settings.json"
LOG_FILE = BASE_DIR / "qq_emoji_tool.log"
PAGE_SIZE = 12
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
        self.keepers: dict[str, Path] = {}
        self.visible_checks: dict[Path, tk.BooleanVar] = {}
        self.visible_check_widgets: dict[Path, ctk.CTkCheckBox] = {}
        self.keeper_variables: dict[str, tk.StringVar] = {}
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
            "tab": ctk.CTkFont(family=FONT_FAMILY, size=14, weight="normal"),
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
        try:
            self.notebook.configure(segmented_button_font=self.fonts["tab"])
        except ValueError:
            # 兼容尚未公开标签字体参数的旧版 CustomTkinter。
            self.notebook._segmented_button.configure(font=self.fonts["tab"])
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
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(10, 6))
        ctk.CTkLabel(head, text="多个文件夹一起扫描，每组保留一份", font=self.fonts["section"],
                     text_color=PALETTE["ink"]).pack(anchor="w")
        ctk.CTkLabel(
            head, text="同一文件夹内部、不同文件夹之间的重复都会列出。可只添加一个文件夹，也可添加多个。",
            font=self.fonts["small"], text_color=PALETTE["muted"], anchor="w", justify="left",
        ).pack(anchor="w", pady=(5, 0))

        saved_folders = self.settings.get("scan_folders")
        if not isinstance(saved_folders, list):
            saved_folders = [self._saved_text("folder_a", ""), self._saved_text("folder_b", "")]
        self.scan_folders: list[Path] = []
        for text in saved_folders:
            if isinstance(text, str) and text.strip():
                folder = Path(os.path.expandvars(text.strip().strip('"'))).expanduser().resolve()
                if folder not in self.scan_folders:
                    self.scan_folders.append(folder)
        folder_actions = ctk.CTkFrame(tab, fg_color="transparent")
        folder_actions.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 6))
        self._make_button(folder_actions, "添加文件夹", self._choose_scan_folder, width=108).pack(side="left")
        self._make_button(folder_actions, "批量添加路径", self._paste_scan_folders, width=116).pack(
            side="left", padx=6)
        self._make_button(folder_actions, "清空列表", self._clear_scan_folders, width=88).pack(side="left")
        ctk.CTkLabel(folder_actions, text="靠前文件夹优先 · 同一文件夹按相对路径排序 · ↑ ↓ 调整顺序",
                     font=self.fonts["small"], text_color=PALETTE["muted"]).pack(side="right")
        folder_panel = ctk.CTkFrame(tab, height=88, fg_color="transparent")
        folder_panel.grid(row=2, column=0, sticky="ew", padx=18)
        folder_panel.grid_columnconfigure(0, weight=1)
        folder_panel.grid_rowconfigure(0, weight=1)
        folder_panel.grid_propagate(False)
        self.folder_list = ctk.CTkScrollableFrame(
            folder_panel, height=94, fg_color=PALETTE["surface"], corner_radius=10,
            border_width=1, border_color=PALETTE["border"],
            scrollbar_button_color=PALETTE["border"], scrollbar_button_hover_color=PALETTE["muted"],
        )
        self.folder_list.grid(row=0, column=0, sticky="nsew")
        self.folder_list.grid_columnconfigure(0, weight=1)
        self._render_scan_folders()

        options = ctk.CTkFrame(tab, fg_color="transparent")
        options.grid(row=3, column=0, sticky="ew", padx=18, pady=(8, 8))
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
            width=132, height=36, corner_radius=10, fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], text_color=PALETTE["on_accent"],
        )
        self.scan_button.pack(side="right")

        summary = ctk.CTkFrame(tab, fg_color=PALETTE["surface"], corner_radius=12, border_width=1,
                               border_color=PALETTE["border"])
        summary.grid(row=4, column=0, sticky="ew", padx=18)
        summary.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(summary, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=16, pady=10)
        inner.grid_columnconfigure(0, weight=1)
        self.summary_text = tk.StringVar(value="添加一个或多个文件夹后开始扫描")
        ctk.CTkLabel(inner, textvariable=self.summary_text, font=self.fonts["body_bold"],
                      text_color=PALETTE["ink"], anchor="w", justify="left", wraplength=650).grid(
                          row=0, column=0, sticky="ew")
        self.selection_text = tk.StringVar(value="已勾选 0 个文件")
        ctk.CTkLabel(inner, textvariable=self.selection_text, font=self.fonts["small"],
                     text_color=PALETTE["accent"]).grid(row=0, column=1, sticky="e")
        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.grid(row=5, column=0, sticky="ew", padx=18, pady=(8, 4))
        self._make_button(toolbar, "全选多余副本", self._select_all, width=116).pack(side="left")
        self._make_button(toolbar, "取消全选", self._select_none, width=88).pack(side="left", padx=(6, 8))
        self.delete_button = ctk.CTkButton(
            toolbar, text="删除勾选项（回收站）", font=self.fonts["body"], command=self._delete, state="disabled",
            width=168, height=34, corner_radius=9, fg_color=PALETTE["danger"], hover_color=PALETTE["danger_hover"],
            text_color=PALETTE["on_accent"],
        )
        self.delete_button.pack(side="right")
        self.dedup_button = ctk.CTkButton(
            toolbar, text="一键去重（每组留一份）", font=self.fonts["body_bold"], command=self._deduplicate,
            state="disabled", width=190, height=34, corner_radius=9, fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"], text_color=PALETTE["on_accent"],
        )
        self.dedup_button.pack(side="right", padx=(0, 8))

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
        self.gallery_hint = tk.StringVar(value=f"按重复组展示 · 每页 {PAGE_SIZE} 组")
        ctk.CTkLabel(pager, textvariable=self.gallery_hint, font=self.fonts["small"],
                     text_color=PALETTE["muted"]).pack(side="right")

        self.gallery = ctk.CTkScrollableFrame(
            tab, fg_color=PALETTE["surface"], border_width=1, border_color=PALETTE["border"],
            corner_radius=12, scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["muted"],
        )
        self.gallery.grid(row=7, column=0, sticky="nsew", padx=18, pady=(6, 0))
        self._render_results()

    def _render_scan_folders(self) -> None:
        for child in self.folder_list.winfo_children():
            child.destroy()
        if not self.scan_folders:
            ctk.CTkLabel(self.folder_list, text="点击“添加文件夹”，或批量粘贴多个文件夹路径。",
                         font=self.fonts["small"], text_color=PALETTE["muted"]).grid(
                             row=0, column=0, pady=26)
        for index, folder in enumerate(self.scan_folders):
            row = ctk.CTkFrame(self.folder_list, fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", padx=6, pady=3)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=f"文件夹 {index + 1}", width=78, anchor="w", font=self.fonts["small"],
                         text_color=PALETTE["ink_soft"]).grid(row=0, column=0, padx=(2, 6))
            entry = ctk.CTkEntry(row, height=30, font=self.fonts["small"], fg_color=PALETTE["raised"],
                                 border_color=PALETTE["border"], text_color=PALETTE["ink"])
            entry.insert(0, str(folder))
            entry.configure(state="readonly")
            entry.grid(row=0, column=1, sticky="ew")
            up = self._make_button(row, "↑", lambda i=index: self._move_scan_folder(i, -1), width=34)
            up.grid(row=0, column=2, padx=(6, 3))
            down = self._make_button(row, "↓", lambda i=index: self._move_scan_folder(i, 1), width=34)
            down.grid(row=0, column=3, padx=3)
            self._set_enabled(up, index > 0)
            self._set_enabled(down, index + 1 < len(self.scan_folders))
            self._make_button(row, "移除", lambda i=index: self._remove_scan_folder(i), width=58).grid(
                row=0, column=4, padx=(3, 0))

    def _add_scan_folders(self, texts: list[str]) -> bool:
        if self.busy:
            return False
        try:
            folders = prepare_folders([Path(os.path.expandvars(text.strip().strip('"'))).expanduser()
                                       for text in texts if text.strip()])
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法添加文件夹", str(exc), parent=self)
            return False
        existing = {normalized(folder) for folder in self.scan_folders}
        added = [folder for folder in folders if normalized(folder) not in existing]
        if added:
            self.scan_folders.extend(added)
            self._invalidate_results()
            self._render_scan_folders()
            self._save_settings()
        self.status.set(f"已添加 {len(added)} 个文件夹，当前共 {len(self.scan_folders)} 个；相同路径自动忽略。")
        return True

    def _choose_scan_folder(self) -> None:
        if self.busy:
            return
        initial = self.scan_folders[-1] if self.scan_folders and self.scan_folders[-1].is_dir() else BASE_DIR
        chosen = filedialog.askdirectory(parent=self, title="添加要一起扫描的文件夹", initialdir=str(initial),
                                         mustexist=True)
        if chosen:
            self._add_scan_folders([chosen])

    def _paste_scan_folders(self) -> None:
        if self.busy:
            return
        window = ctk.CTkToplevel(self)
        window.title("批量添加文件夹")
        window.geometry("760x410")
        window.configure(fg_color=PALETTE["bg"])
        ctk.CTkLabel(window, text="每行填写一个文件夹的完整路径，按粘贴顺序加入保留优先级列表。",
                     font=self.fonts["body"], text_color=PALETTE["ink"]).pack(anchor="w", padx=20, pady=16)
        paths = ctk.CTkTextbox(window, font=self.fonts["small"], fg_color=PALETTE["surface"],
                               text_color=PALETTE["ink"], border_width=1, border_color=PALETTE["border"])
        paths.pack(fill="both", expand=True, padx=20)

        def add() -> None:
            if self._add_scan_folders(paths.get("1.0", "end").splitlines()):
                window.destroy()

        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=16)
        self._make_button(actions, "取消", window.destroy, width=88).pack(side="right")
        self._make_button(actions, "添加这些文件夹", add, width=140).pack(side="right", padx=8)
        window.transient(self)
        window.grab_set()
        paths.focus_set()

    def _move_scan_folder(self, index: int, delta: int) -> None:
        target = index + delta
        if self.busy or not 0 <= target < len(self.scan_folders):
            return
        self.scan_folders[index], self.scan_folders[target] = self.scan_folders[target], self.scan_folders[index]
        self._folders_changed()

    def _remove_scan_folder(self, index: int) -> None:
        if not self.busy:
            self.scan_folders.pop(index)
            self._folders_changed()

    def _clear_scan_folders(self) -> None:
        if not self.busy:
            self.scan_folders.clear()
            self._folders_changed()

    def _folders_changed(self) -> None:
        self._invalidate_results()
        self._render_scan_folders()
        self._save_settings()

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
            "scan_folders": [str(folder) for folder in self.scan_folders],
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
        self.notebook.set("02   查找重复文件")
        self._add_scan_folders([self.output_folder.get()])

    # ------------------------------------------------------------ 扫描流程

    def _scan(self) -> None:
        if self.busy:
            return
        try:
            roots = prepare_folders(self.scan_folders)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法扫描", str(exc), parent=self)
            return
        self._invalidate_results()
        self._save_settings()
        recursive = self.recursive.get()
        self._log(f"开始扫描 {len(roots)} 个文件夹；包含子文件夹 = {recursive}")
        for index, root in enumerate(roots, 1):
            self._log(f"文件夹 {index}（保留优先级 {index}）：{root}")
        self._start_job("扫描重复文件",
                        lambda cancel, report: find_duplicates(roots, recursive, cancel, report),
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
        self.keepers = default_keepers(result)
        self.checked = {record.path for record in dedup_candidates(result, self.keepers)}
        self.page = 0
        self._update_summary()
        self._render_results()
        count = len(result.files)
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
        self.keepers.clear()
        self.page = 0
        self.summary_text.set("添加一个或多个文件夹后开始扫描")
        self._render_results()

    def _update_summary(self) -> None:
        if self.scan_result is None:
            return
        groups = self.scan_result.groups
        candidates = dedup_candidates(self.scan_result, self.keepers)
        count = sum(len(records) for records in groups.values())
        self.summary_text.set(f"{len(groups)} 组重复 · {count} 份文件 · "
                              f"可去重 {len(candidates)} 份（{format_size(sum(r.size for r in candidates))}）")

    def _duplicate_records(self) -> list[FileRecord]:
        if self.scan_result is None:
            return []
        return dedup_candidates(self.scan_result, self.keepers)

    def _choose_keeper(self, digest: str, path: Path) -> None:
        if self.busy:
            return
        if self.scan_result is None or not any(record.path == path for record in self.scan_result.groups[digest]):
            return
        previous = self.keepers[digest]
        self.keepers[digest] = path
        self.checked.discard(path)
        if previous != path:
            self.checked.add(previous)
        if digest in self.keeper_variables:
            self.keeper_variables[digest].set(str(path))
        for record in self.scan_result.groups[digest]:
            if record.path in self.visible_checks:
                self.visible_checks[record.path].set(record.path in self.checked)
                self._set_enabled(self.visible_check_widgets[record.path], record.path != path)
        self._update_selection()

    # ------------------------------------------------------------ 结果画廊

    def _render_results(self) -> None:
        self.thumbnail_cancel.set()
        self.thumbnail_cancel = threading.Event()
        self.thumbnail_generation += 1
        generation = self.thumbnail_generation
        for child in self.gallery.winfo_children():
            child.destroy()
        self.visible_checks.clear()
        self.visible_check_widgets.clear()
        self.keeper_variables.clear()
        self.thumbnail_labels.clear()
        self.thumbnail_photos.clear()
        groups = list(self.scan_result.groups.items()) if self.scan_result else []
        page_count = math.ceil(len(groups) / PAGE_SIZE)
        self.page = min(self.page, max(0, page_count - 1))
        self.page_text.set(f"第 {self.page + 1 if groups else 0} / {page_count} 页")
        self._set_enabled(self.previous_button, self.page > 0 and not self.busy)
        self._set_enabled(self.next_button, self.page + 1 < page_count and not self.busy)
        if self.scan_result:
            self.target_hint.set(f"已扫描 {len(self.scan_result.roots)} 个文件夹、{len(self.scan_result.files)} 个文件；"
                                 "每组可改选保留副本。一键去重覆盖所有页。")
        else:
            self.target_hint.set("每组可指定保留哪一份；清理时将其余副本移入回收站。")
        self._update_selection()
        self.gallery.grid_columnconfigure(0, weight=1)
        if not groups:
            message = ("所选文件夹中没有发现重复内容。" if self.scan_result
                       else "扫描完成后，会按组展示所有重复文件及它们的位置。")
            ctk.CTkLabel(self.gallery, text=message, font=self.fonts["body"], text_color=PALETTE["muted"],
                          justify="center", wraplength=620).grid(row=0, column=0, pady=60)
            return
        page_groups = groups[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        page_records = []
        for index, (digest, records) in enumerate(page_groups):
            keeper = next(record for record in records if record.path == self.keepers[digest])
            page_records.append(keeper)
            self._build_group(index, digest, records, keeper)
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

    def _group_locations(self, records: list[FileRecord]) -> str:
        counts: dict[Path, int] = {}
        for record in records:
            counts[record.root] = counts.get(record.root, 0) + 1
        if len(counts) == 1:
            kind = "文件夹内重复"
        elif any(count > 1 for count in counts.values()):
            kind = "跨文件夹及文件夹内重复"
        else:
            kind = "跨文件夹重复"
        locations = "，".join(f"文件夹 {self.scan_result.roots.index(root) + 1} × {count}"
                              for root, count in counts.items())
        return f"{kind} · {locations}"

    def _build_group(self, index: int, digest: str, records: list[FileRecord], keeper: FileRecord) -> None:
        card = ctk.CTkFrame(self.gallery, fg_color=PALETTE["raised"], corner_radius=12, border_width=1,
                             border_color=PALETTE["border"])
        card.grid(row=index, column=0, sticky="ew", padx=6, pady=6)
        card.grid_columnconfigure(0, weight=1)
        heading = ctk.CTkFrame(card, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 5))
        heading.grid_columnconfigure(0, weight=1)
        number = self.page * PAGE_SIZE + index + 1
        ctk.CTkLabel(heading, text=f"第 {number} 组 · {len(records)} 份相同内容 · 留 1 份，去重 {len(records) - 1} 份",
                     font=self.fonts["body_bold"], text_color=PALETTE["ink"], anchor="w",
                     justify="left", wraplength=700).grid(row=0, column=0, sticky="ew")
        self._make_button(heading, "预览及完整路径", lambda r=keeper: self._preview(r), width=134).grid(
            row=0, column=1, padx=(8, 0))
        ctk.CTkLabel(card, text=self._group_locations(records), font=self.fonts["small"],
                     text_color=PALETTE["accent"], anchor="w", justify="left", wraplength=870).grid(
                         row=1, column=0, sticky="ew", padx=14, pady=(0, 6))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        body.grid_columnconfigure(1, weight=1)
        label = ctk.CTkLabel(
            body, text="正在加载缩略图…", font=self.fonts["small"], width=THUMB_SIZE[0], height=THUMB_SIZE[1],
            corner_radius=8, fg_color=PALETTE["placeholder"], text_color=PALETTE["muted"],
        )
        label.grid(row=0, column=0, sticky="n", padx=(0, 14), pady=5)
        label.bind("<Button-1>", lambda _event, r=keeper: self._preview(r))
        label.configure(cursor="hand2")
        self.thumbnail_labels[keeper.path] = label
        copies = ctk.CTkFrame(body, fg_color="transparent")
        copies.grid(row=0, column=1, sticky="ew")
        copies.grid_columnconfigure(0, weight=1)
        keep_variable = tk.StringVar(value=str(keeper.path))
        self.keeper_variables[digest] = keep_variable
        for offset, record in enumerate(records):
            row = ctk.CTkFrame(copies, fg_color=PALETTE["surface"], corner_radius=8)
            row.grid(row=offset, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(2, weight=1)
            ctk.CTkRadioButton(
                row, text="保留此份", variable=keep_variable, value=str(record.path), width=100,
                command=lambda d=digest, p=record.path: self._choose_keeper(d, p),
                font=self.fonts["small"], text_color=PALETTE["ink"], fg_color=PALETTE["accent"],
                border_color=PALETTE["muted"], radiobutton_width=18, radiobutton_height=18,
            ).grid(row=0, column=0, sticky="w", padx=(10, 6), pady=(7, 2))
            variable = tk.BooleanVar(value=record.path in self.checked)
            self.visible_checks[record.path] = variable
            check = ctk.CTkCheckBox(
                row, text="删除", variable=variable, width=78,
                command=lambda r=record, v=variable: self._toggle_record(r, v.get()),
                font=self.fonts["small"], text_color=PALETTE["ink"], fg_color=PALETTE["danger"],
                hover_color=PALETTE["danger_hover"], border_color=PALETTE["muted"],
                checkbox_width=18, checkbox_height=18, corner_radius=5,
            )
            check.grid(row=0, column=1, sticky="w", pady=(7, 2))
            self.visible_check_widgets[record.path] = check
            self._set_enabled(check, record.path != keeper.path)
            root_number = self.scan_result.roots.index(record.root) + 1
            ctk.CTkLabel(row, text=f"文件夹 {root_number} · {format_size(record.size)}", font=self.fonts["tiny"],
                         text_color=PALETTE["muted"], anchor="w").grid(row=0, column=2, sticky="w", pady=(7, 2))
            ctk.CTkLabel(row, text=str(record.path), font=self.fonts["small"], text_color=PALETTE["ink_soft"],
                         anchor="w", justify="left", wraplength=620).grid(
                             row=1, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 7))

    def _toggle_record(self, record: FileRecord, checked: bool) -> None:
        if self.busy:
            return
        if checked and record.path != self.keepers.get(record.digest):
            self.checked.add(record.path)
        else:
            self.checked.discard(record.path)
        self._update_selection()

    def _select_all(self) -> None:
        if self.busy:
            return
        self.checked = {record.path for record in self._duplicate_records()}
        for path, variable in self.visible_checks.items():
            variable.set(path in self.checked)
        self._update_selection()

    def _select_none(self) -> None:
        if self.busy:
            return
        self.checked.clear()
        for variable in self.visible_checks.values():
            variable.set(False)
        self._update_selection()

    def _update_selection(self) -> None:
        records = [record for record in self._duplicate_records() if record.path in self.checked]
        total = sum(record.size for record in records)
        self.selection_text.set(f"已勾选 {len(records)} 个 · {format_size(total)}")
        self._set_enabled(self.delete_button, bool(records) and not self.busy)
        self._set_enabled(self.dedup_button, bool(self.scan_result and self.scan_result.groups) and not self.busy)

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
        ctk.CTkLabel(body, text="本组所有副本及保留位置", font=self.fonts["body_bold"],
                     text_color=PALETTE["ink"], anchor="w").pack(anchor="w", pady=(16, 6))
        matches = ctk.CTkTextbox(body, height=110, corner_radius=10, fg_color=PALETTE["surface"],
                                 border_width=1, border_color=PALETTE["border"], text_color=PALETTE["ink_soft"],
                                 font=self.fonts["small"])
        matches.pack(fill="both", expand=True)
        group = self.scan_result.groups[record.digest]
        matches.insert("end", self._group_locations(group) + "\n\n")
        for candidate in group:
            kind = "保留副本" if candidate.path == self.keepers[record.digest] else "多余副本"
            matches.insert("end", f"【{kind}】{candidate.path}\n")
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
        selected = [record for record in self._duplicate_records() if record.path in self.checked]
        self._confirm_cleanup(selected, all_duplicates=False)

    def _deduplicate(self) -> None:
        if self.busy or self.scan_result is None:
            return
        self._confirm_cleanup(self._duplicate_records(), all_duplicates=True)

    def _confirm_cleanup(self, selected: list[FileRecord], all_duplicates: bool) -> None:
        if not selected:
            return
        scan = self.scan_result
        keepers = dict(self.keepers)
        size = format_size(sum(record.size for record in selected))
        digests = list(dict.fromkeys(record.digest for record in selected))
        locations = "\n".join(str(keepers[digest]) for digest in digests[:6])
        if len(digests) > 6:
            locations += f"\n……另 {len(digests) - 6} 组的保留位置见结果列表。"
        action = "一键去重（覆盖所有页）" if all_duplicates else "清理勾选项"
        confirmed = messagebox.askyesno(
            "确认移入回收站",
            f"{action}：将 {len(digests)} 组中的 {len(selected)} 份多余副本（{size}）移入回收站。\n\n"
            f"每组指定的以下副本保留：\n{locations}\n\n"
            "删除前会重新校验待删文件和保留副本。保留副本丢失或发生变化时跳过删除。是否继续？",
            parent=self, icon="warning", default="no",
        )
        if not confirmed:
            return
        self.thumbnail_cancel.set()
        self._log(f"{action}：用户确认将 {len(selected)} 份多余副本移入回收站。")
        for digest in digests:
            self._log(f"指定保留：{keepers[digest]}")
        self._start_job("清理重复文件",
                        lambda cancel, report: delete_duplicates(scan, selected, keepers, cancel, report),
                        self._deleted)

    def _deleted(self, result: OperationResult) -> None:
        removed = set(result.completed)
        if self.scan_result:
            self.scan_result.files = [record for record in self.scan_result.files if record.path not in removed]
            groups = {}
            for digest, records in self.scan_result.groups.items():
                updated = [record for record in records if record.path not in removed]
                if len(updated) > 1:
                    groups[digest] = updated
            self.scan_result.groups = groups
            self.keepers = {digest: self.keepers[digest] for digest in groups}
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
            messagebox.showinfo(title, text + "\n\n各组指定的保留副本已保留。详细结果见“操作记录”。", parent=self)

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
            elif previous.get("state") == "readonly":
                widget.configure(state="readonly")
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
        # 取消本解释器的定时执行，但由各控件销毁自己的已注册命令。
        # 根窗口的 after_cancel 会提前删除子控件命令，导致销毁时重复删除。
        for after_id in self.tk.splitlist(self.tk.call("after", "info")):
            self.tk.call("after", "cancel", after_id)
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
