"""QQ 表情包工具的文件操作；不依赖图形界面。"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

CHUNK_SIZE = 1024 * 1024
EMOJI_SUFFIX = Path("nt_qq/nt_data/Emoji/personal_emoji/Ori")
Reporter = Callable[[str, object], None]


class Cancelled(Exception):
    """用户取消后台操作。"""


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled()


def normalized(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def overlaps(first: Path, second: Path) -> bool:
    return is_inside(first, second) or is_inside(second, first)


def is_link(path: Path) -> bool:
    info = path.lstat()
    # 只排除符号链接和目录联接，允许 OneDrive 的普通云文件重解析点。
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in (
        0xA0000003,
        0xA000000C,
    )


def signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    # Windows Python 3.14.7 的 stat / fstat 对 st_ctime_ns 的解释不一致。
    # 使用创建时间，避免把未改变的文件误判为变化；旧 Python 无此字段时
    # 仍比较文件 ID、大小和修改时间，删除前还会重新计算完整内容哈希。
    changed = getattr(info, "st_birthtime_ns", 0) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed)


def readable_directory(text: str) -> Path:
    if not text.strip():
        raise ValueError("请选择文件夹。")
    path = Path(os.path.expandvars(text.strip().strip('"'))).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"文件夹不存在或无法访问：\n{path}")
    return path


def default_search_roots() -> list[Path]:
    home = Path.home()
    candidates = [home / "Documents" / "Tencent Files"]
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                personal = winreg.QueryValueEx(key, "Personal")[0]
            candidates.insert(0, Path(os.path.expandvars(personal)) / "Tencent Files")
        except (OSError, ImportError):
            pass
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Documents" / "Tencent Files")
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = normalized(path)
        if key not in seen:
            result.append(path)
            seen.add(key)
    return result


@dataclass(frozen=True)
class Account:
    qq: str
    folder: Path
    count: int


@dataclass(frozen=True)
class FileRecord:
    path: Path
    relative: str
    size: int
    digest: str
    stamp: tuple[int, int, int, int, int]


@dataclass
class ScanResult:
    roots: dict[str, Path]
    files: dict[str, list[FileRecord]]
    groups: dict[str, dict[str, list[FileRecord]]]
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class OperationResult:
    completed: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def walk_files(
    root: Path, recursive: bool, cancel: threading.Event, errors: list[str]
) -> Iterator[Path]:
    """扫描普通文件，不沿目录联接或符号链接进入其他目录。"""
    pending = [root]
    while pending:
        check_cancel(cancel)
        current = pending.pop()
        try:
            if is_link(current):
                errors.append(f"跳过链接目录：{current}")
                continue
            with os.scandir(current) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    path = Path(entry.path)
                    try:
                        if is_link(path):
                            errors.append(f"跳过符号链接或目录联接：{path}")
                        elif entry.is_file(follow_symlinks=False):
                            yield path
                        elif recursive and entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                    except OSError as exc:
                        errors.append(f"无法读取 {path}：{exc}")
        except OSError as exc:
            errors.append(f"无法扫描 {current}：{exc}")


def detect_accounts(
    roots: list[Path], cancel: threading.Event, report: Reporter
) -> tuple[list[Account], list[str]]:
    errors: list[str] = []
    accounts: list[Account] = []
    seen: set[str] = set()
    for index, root in enumerate(roots, 1):
        check_cancel(cancel)
        report("progress", (index - 1, len(roots), f"查找 QQ 账号：{root}"))
        if not root.is_dir():
            continue
        candidates: list[tuple[str, Path]] = []
        try:
            # 可选择 Tencent Files、它的父目录、QQ 账号目录，或直接选择 Ori。
            if root.name.casefold() == "ori" and root.parent.name == "personal_emoji":
                qq = next((p.name for p in root.parents if p.name.isdecimal()), "手动目录")
                candidates.append((qq, root))
            if root.name.isdecimal():
                candidates.append((root.name, root / EMOJI_SUFFIX))
            for base in (root, root / "Tencent Files", root / "nt_qq"):
                if not base.is_dir():
                    continue
                for child in base.iterdir():
                    check_cancel(cancel)
                    if child.name.isdecimal() and child.is_dir() and not is_link(child):
                        candidates.extend(
                            [
                                (child.name, child / EMOJI_SUFFIX),
                                (child.name, child / "nt_data/Emoji/personal_emoji/Ori"),
                            ]
                        )
            for qq, folder in candidates:
                check_cancel(cancel)
                if not folder.is_dir() or is_link(folder):
                    continue
                key = normalized(folder)
                if key in seen:
                    continue
                seen.add(key)
                count = sum(1 for _ in walk_files(folder, True, cancel, errors))
                if count:
                    accounts.append(Account(qq, folder.resolve(), count))
        except OSError as exc:
            errors.append(f"无法访问 {root}：{exc}")
    accounts.sort(key=lambda account: (account.qq, str(account.folder)))
    report("progress", (len(roots), len(roots), f"发现 {len(accounts)} 个有本地表情的账号目录"))
    return accounts, errors


def ensure_regular_under(path: Path, root: Path) -> None:
    if not is_inside(path, root) or path == root:
        raise ValueError(f"文件不在所选文件夹中：{path}")
    current = path
    while True:
        if is_link(current):
            raise ValueError(f"跳过符号链接或目录联接：{current}")
        if current == root:
            break
        if current.parent == current:
            raise ValueError(f"文件路径已改变：{path}")
        current = current.parent
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"不是普通文件：{path}")


def hash_file(path: Path, root: Path, cancel: threading.Event) -> FileRecord:
    check_cancel(cancel)
    ensure_regular_under(path, root)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise ValueError("读取前文件发生变化，请重新扫描。")
        while True:
            check_cancel(cancel)
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    if signature(before) != signature(after) or signature(path.stat()) != signature(after):
        raise ValueError("读取期间文件发生变化，请重新扫描。")
    return FileRecord(path, str(path.relative_to(root)), before.st_size, digest.hexdigest(), signature(after))


def export_emojis(
    source: Path, destination: Path, cancel: threading.Event, report: Reporter
) -> OperationResult:
    if overlaps(source, destination):
        raise ValueError("导出目录和表情库不能相同，也不能互相包含。请另外选择一个文件夹。")
    destination.mkdir(parents=True, exist_ok=True)
    if is_link(destination):
        raise ValueError("导出目录已变成符号链接或目录联接，请重新选择。")
    result = OperationResult()
    try:
        report("progress", (0, 0, "正在整理要导出的表情…"))
        files = list(walk_files(source, True, cancel, result.errors))
        for index, path in enumerate(files, 1):
            check_cancel(cancel)
            relative = path.relative_to(source).with_suffix(".gif")
            target = destination / relative
            created = False
            succeeded = False
            try:
                ensure_regular_under(path, source)
                if not is_inside(target, destination):
                    raise ValueError("输出路径包含指向导出目录外的链接。")
                # 从内向外验证每个父目录后才创建下一层目录。
                parent = destination
                for part in relative.parts[:-1]:
                    if is_link(parent):
                        raise ValueError(f"输出路径包含目录联接：{parent}")
                    parent = parent / part
                    parent.mkdir(exist_ok=True)
                    if is_link(parent):
                        raise ValueError(f"输出路径包含目录联接：{parent}")
                before = path.stat()
                with path.open("rb") as incoming:
                    if signature(os.fstat(incoming.fileno())) != signature(before):
                        raise ValueError("源文件发生变化，请重试。")
                    # x 模式既不覆盖原文件，也不会为文件名追加数字。
                    with target.open("xb") as outgoing:
                        created = True
                        while True:
                            check_cancel(cancel)
                            block = incoming.read(CHUNK_SIZE)
                            if not block:
                                break
                            outgoing.write(block)
                        if signature(os.fstat(incoming.fileno())) != signature(before):
                            raise ValueError("复制时源文件发生变化，请重试。")
                if signature(path.stat()) != signature(before):
                    raise ValueError("复制时源文件发生变化，请重试。")
                succeeded = True
                result.completed.append(target)
            except FileExistsError:
                result.skipped.append(f"同名文件已存在，跳过：{target}")
            except (OSError, ValueError) as exc:
                result.errors.append(f"导出失败 {path}：{exc}")
            finally:
                if created and not succeeded:
                    try:
                        target.unlink()
                    except OSError as exc:
                        result.errors.append(f"未完成的副本清理失败 {target}：{exc}")
            report("progress", (index, len(files), f"导出 {index}/{len(files)} · {path.name}"))
    except Cancelled:
        result.cancelled = True
    return result


def find_duplicates(
    first: Path, second: Path, recursive: bool, cancel: threading.Event, report: Reporter
) -> ScanResult:
    if overlaps(first, second):
        raise ValueError("两个文件夹不能相同，也不能互相包含。")
    roots = {"A": first, "B": second}
    result = ScanResult(roots=roots, files={"A": [], "B": []}, groups={})
    try:
        pending: list[tuple[str, Path]] = []
        for side, root in roots.items():
            report("progress", (0, 0, f"正在列出文件夹 {side} 的文件…"))
            for path in walk_files(root, recursive, cancel, result.errors):
                pending.append((side, path))
        by_hash: dict[str, dict[str, list[FileRecord]]] = {
            "A": defaultdict(list), "B": defaultdict(list)
        }
        for index, (side, path) in enumerate(pending, 1):
            check_cancel(cancel)
            report("progress", (index - 1, len(pending), f"计算 SHA-256 · {side} · {path.name}"))
            try:
                record = hash_file(path, roots[side], cancel)
                result.files[side].append(record)
                by_hash[side][record.digest].append(record)
            except (OSError, ValueError) as exc:
                result.errors.append(f"读取失败 {path}：{exc}")
            report("progress", (index, len(pending), f"已计算 {index}/{len(pending)} 个文件"))
        for digest in sorted(by_hash["A"].keys() & by_hash["B"].keys()):
            result.groups[digest] = {side: by_hash[side][digest] for side in ("A", "B")}
    except Cancelled:
        result.cancelled = True
        # 取消时不展示尚未完整扫描的结果，避免把部分结果当成全量。
        result.groups.clear()
    return result


def delete_duplicates(
    scan: ScanResult,
    side: str,
    selected: list[FileRecord],
    cancel: threading.Event,
    report: Reporter,
) -> OperationResult:
    from send2trash import send2trash

    result = OperationResult()
    other = "B" if side == "A" else "A"
    if scan.cancelled or overlaps(scan.roots["A"], scan.roots["B"]):
        raise ValueError("扫描结果无效，请重新扫描。")
    try:
        for index, record in enumerate(selected, 1):
            check_cancel(cancel)
            report("progress", (index - 1, len(selected), f"校验并移入回收站 · {record.path.name}"))
            try:
                group = scan.groups.get(record.digest)
                if not group or record not in group[side]:
                    raise ValueError("该文件不在本次重复文件结果中。")
                current = hash_file(record.path, scan.roots[side], cancel)
                if current.stamp != record.stamp or current.digest != record.digest:
                    raise ValueError("文件自扫描后已变化，请重新扫描。")
                # 每次删除前重新检查另一侧至少有一份内容相同的文件。
                keeper = None
                for candidate in group[other]:
                    try:
                        copy = hash_file(candidate.path, scan.roots[other], cancel)
                        if copy.digest == record.digest and copy.size == record.size:
                            keeper = copy
                            break
                    except (OSError, ValueError):
                        continue
                if keeper is None:
                    raise ValueError("另一侧已没有可读取的相同文件，已跳过。")
                check_cancel(cancel)
                ensure_regular_under(record.path, scan.roots[side])
                ensure_regular_under(keeper.path, scan.roots[other])
                if signature(record.path.stat()) != current.stamp or signature(keeper.path.stat()) != keeper.stamp:
                    raise ValueError("校验后文件发生变化，已跳过。")
                # 回收失败时报告错误，不回退到永久删除。
                send2trash(str(record.path))
                result.completed.append(record.path)
            except Exception as exc:
                if isinstance(exc, Cancelled):
                    raise
                result.errors.append(f"未删除 {record.path}：{exc}")
            report("progress", (index, len(selected), f"已处理 {index}/{len(selected)} 个文件"))
    except Cancelled:
        result.cancelled = True
    return result
