"""Gen Image Tool - giao diện (ttkbootstrap).

Chức năng:
  - Thêm job: chọn 1 thư mục ảnh (chứa sample + ảnh biến thể) + nhập prompt
    (gõ tay hoặc chọn file .txt).
  - Chạy tất cả job đã thêm, chạy song song, log tiến trình realtime có màu.
  - Kết quả lưu vào output/<ten_job>/ cạnh file chạy, có nút mở nhanh.
"""

import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import *
from PIL import Image, ImageTk

import core
import video_core

try:
    import winsound
except ImportError:
    winsound = None

try:
    from win11toast import notify as win_toast_notify
except ImportError:
    win_toast_notify = None

try:
    from playsound import playsound
except ImportError:
    playsound = None

BASE_DIR = Path(sys.argv[0]).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
MAX_WORKERS = 7

ASSETS_DIR = BASE_DIR / "assets"
SOUND_OK = ASSETS_DIR / "xong.mp3"
SOUND_ERR = ASSETS_DIR / "loi.mp3"
BADGE_ICON = ASSETS_DIR / "red_dot.ico"
ACCOUNTS_FILE = BASE_DIR / core.ACCOUNTS_FILENAME

DEFAULT_LOGO_PATH = ASSETS_DIR / "Logo bay 010.mov"
DEFAULT_OUTTRO_PATH = ASSETS_DIR / "outtro 010.mp4"

# Nhãn tiếng Việt -> tên transition thật của ffmpeg xfade (dùng trong video_core.py).
TRANSITION_OPTIONS = {
    "Trượt trái → phải": "slideright",
    "Trượt phải → trái": "slideleft",
    "Trượt lên (Swipe Up)": "slideup",
    "Trượt xuống (Swipe Down)": "slidedown",
    "Mờ dần (fade qua đen, không chồng ảnh)": "fadeblack",
    "Nháy sáng (Flash Transition)": "fadewhite",
    "Hoà tan (Dissolve)": "dissolve",
    "Mặt nạ tròn - mở (Mask Reveal)": "circleopen",
    "Mặt nạ tròn - đóng (Mask Reveal)": "circleclose",
    "Mặt nạ chữ nhật (Mask Reveal)": "rectcrop",
    "Nhoè chuyển động (Motion Blur)": "hblur",
    "Chia màn hình dọc (Split Transition)": "vuslice",
    "Chia màn hình ngang (Split Transition)": "hlslice",
    "Tan pixel (Pixel Dissolve)": "pixelize",
    "Phóng to khi chuyển (giả lập Parallax)": "zoomin",
    "Gạt từ trái": "wipeleft",
    "Gạt từ phải": "wiperight",
}


def set_taskbar_badge(hwnd: int, show: bool) -> None:
    """Overlay icon (chấm đỏ) trên icon taskbar Windows, kiểu thông báo Discord.

    Gọi ITaskbarList3::SetOverlayIcon qua COM vtable trực tiếp bằng ctypes
    (slot 18, sau IUnknown[0-2] + ITaskbarList[3-7] + ITaskbarList2[8] + 9 hàm đầu của ITaskbarList3[9-17]).
    """
    if not hwnd:
        return
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        CLSID_TaskbarList = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
        IID_ITaskbarList3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
        CLSCTX_INPROC_SERVER = 1
        SET_OVERLAY_ICON_SLOT = 18

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def guid_from_string(s: str) -> GUID:
            u = uuid.UUID(s)
            b = u.bytes_le
            g = GUID()
            g.Data1 = int.from_bytes(b[0:4], "little")
            g.Data2 = int.from_bytes(b[4:6], "little")
            g.Data3 = int.from_bytes(b[6:8], "little")
            for i in range(8):
                g.Data4[i] = b[8 + i]
            return g

        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32

        clsid = guid_from_string(CLSID_TaskbarList)
        iid = guid_from_string(IID_ITaskbarList3)

        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(ptr)
        )
        if hr != 0 or not ptr.value:
            return

        vtable_ptr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
        vtable = ctypes.cast(vtable_ptr.contents, ctypes.POINTER(ctypes.c_void_p * (SET_OVERLAY_ICON_SLOT + 1)))

        SetOverlayIconProto = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.HICON, wintypes.LPCWSTR
        )
        set_overlay_icon = SetOverlayIconProto(vtable.contents[SET_OVERLAY_ICON_SLOT])

        hicon = 0
        if show and BADGE_ICON.exists():
            hicon = shell32.ExtractIconW(None, str(BADGE_ICON), 0)
            if hicon in (0, 1):
                hicon = 0

        set_overlay_icon(ptr.value, hwnd, hicon, "Có thông báo mới")
    except Exception:
        pass


def _play_sound_async(path: Path) -> None:
    if not playsound or not path.exists():
        return

    def _run():
        try:
            playsound(str(path))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


class AddJobDialog(tb.Toplevel):
    def __init__(self, master, initial=None):
        super().__init__(master)
        editing = initial is not None
        self.title("Sửa job" if editing else "Thêm job mới")
        self.geometry("560x520")
        self.minsize(480, 420)
        self.resizable(True, True)
        self.result = None

        pad = {"padx": 16, "pady": (14, 4)}

        # Hàng nút bấm dưới cùng - pack TRƯỚC, side=bottom, để luôn hiện được dù
        # màn hình nhỏ/tỉ lệ hiển thị (DPI) khác máy khiến nội dung bên trên bị dài ra.
        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="📄 Nạp từ file .txt", bootstyle="secondary-outline",
                  command=self.load_prompt_file).pack(side="left")
        tb.Button(btn_row, text="Huỷ", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="💾 Lưu thay đổi" if editing else "✓ Thêm job", bootstyle="success",
                  command=self.confirm).pack(side="right", padx=8)

        tb.Label(self, text="Tên job", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.name_var = tb.StringVar(value=initial["name"] if editing else "")
        tb.Entry(self, textvariable=self.name_var).pack(fill="x", padx=16)

        tb.Label(self, text="Backend gen ảnh", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.backend_var = tb.StringVar(
            value="Muse2API" if (editing and initial.get("backend") == "muse") else "Codex CLI"
        )
        tb.Combobox(self, textvariable=self.backend_var, state="readonly",
                    values=["Codex CLI", "Muse2API"]).pack(fill="x", padx=16)

        tb.Label(self, text="Thư mục lưu ảnh kết quả (bỏ trống = dùng thư mục mặc định của tool)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        out_row = tb.Frame(self)
        out_row.pack(fill="x", padx=16)
        self.output_dir_var = tb.StringVar(
            value=str(initial["output_dir"]) if editing and initial.get("output_dir") else ""
        )
        tb.Entry(out_row, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(out_row, text="📁 Chọn...", bootstyle="secondary",
                  command=self.pick_output_dir).pack(side="left", padx=(8, 0))
        tb.Button(out_row, text="✕ Mặc định", bootstyle="secondary-outline",
                  command=lambda: self.output_dir_var.set("")).pack(side="left", padx=(8, 0))

        tb.Label(self, text="Thư mục ảnh (chứa các ảnh biến thể, mỗi ảnh tự gen riêng với prompt)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        folder_row = tb.Frame(self)
        folder_row.pack(fill="x", padx=16)
        self.folder_var = tb.StringVar(value=str(initial["folder"]) if editing else "")
        tb.Entry(folder_row, textvariable=self.folder_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(folder_row, text="📁 Chọn thư mục", bootstyle="secondary",
                  command=self.pick_folder).pack(side="left", padx=(8, 0))

        tb.Label(self, text="Prompt - mô tả yêu cầu gen ảnh",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        text_frame = tb.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=16)
        self.prompt_text = tb.Text(text_frame, height=11, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.pack(side="left", fill="both", expand=True)
        if editing:
            self.prompt_text.insert("1.0", initial["prompt"])
        sb = tb.Scrollbar(text_frame, command=self.prompt_text.yview)
        sb.pack(side="right", fill="y")
        self.prompt_text.configure(yscrollcommand=sb.set)

        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _on_return(self, event):
        if isinstance(event.widget, tb.Text):
            return  # để Enter xuống dòng bình thường trong ô prompt
        self.confirm()

    def pick_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục ảnh")
        if folder:
            self.folder_var.set(folder)
            if not self.name_var.get().strip():
                self.name_var.set(Path(folder).name)

    def pick_output_dir(self):
        folder = filedialog.askdirectory(title="Chọn thư mục lưu ảnh kết quả cho job này")
        if folder:
            self.output_dir_var.set(folder)

    def load_prompt_file(self):
        f = filedialog.askopenfilename(title="Chọn file prompt .txt", filetypes=[("Text files", "*.txt")])
        if f:
            try:
                content = Path(f).read_text(encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("Lỗi", f"Không đọc được file: {exc}")
                return
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", content)

    def confirm(self):
        name = self.name_var.get().strip()
        folder = self.folder_var.get().strip()
        prompt = self.prompt_text.get("1.0", "end").strip()

        if not name:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập tên job.")
            return
        if not folder:
            messagebox.showwarning("Thiếu thông tin", "Hãy chọn thư mục ảnh.")
            return
        if not prompt:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập prompt hoặc nạp từ file .txt.")
            return

        try:
            core.collect_job_images(Path(folder))
        except Exception as exc:
            messagebox.showerror("Lỗi ảnh", str(exc))
            return

        backend = "muse" if self.backend_var.get() == "Muse2API" else "codex"
        self.result = {
            "mode": "image", "name": name, "folder": Path(folder), "prompt": prompt, "backend": backend,
        }
        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            self.result["output_dir"] = Path(output_dir)
        self.destroy()


class AddTextJobDialog(tb.Toplevel):
    """Thêm job KHÔNG cần ảnh mẫu - chỉ cần prompt mô tả, Codex tự vẽ ảnh mới hoàn toàn."""

    def __init__(self, master, initial=None):
        super().__init__(master)
        editing = initial is not None
        self.title("Sửa job (chỉ prompt)" if editing else "Thêm job mới - chỉ cần prompt")
        self.geometry("560x480")
        self.minsize(480, 400)
        self.resizable(True, True)
        self.result = None

        pad = {"padx": 16, "pady": (14, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="📄 Nạp từ file .txt", bootstyle="secondary-outline",
                  command=self.load_prompt_file).pack(side="left")
        tb.Button(btn_row, text="Huỷ", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="💾 Lưu thay đổi" if editing else "✓ Thêm job", bootstyle="success",
                  command=self.confirm).pack(side="right", padx=8)

        tb.Label(
            self,
            text="Job này gen ảnh từ prompt - có thể đính kèm thêm 1 ảnh mẫu (tuỳ chọn)\n"
                 "để Codex/Muse tham khảo, hoặc để trống để tự vẽ ảnh mới hoàn toàn.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 0))

        tb.Label(self, text="Ảnh mẫu đính kèm (tuỳ chọn)", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", **pad
        )
        sample_row = tb.Frame(self)
        sample_row.pack(fill="x", padx=16)
        self.sample_image_var = tb.StringVar(
            value=str(initial["sample_image"]) if editing and initial.get("sample_image") else ""
        )
        tb.Entry(sample_row, textvariable=self.sample_image_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(sample_row, text="🖼 Chọn ảnh...", bootstyle="secondary",
                  command=self.pick_sample_image).pack(side="left", padx=(8, 0))
        tb.Button(sample_row, text="✕ Bỏ ảnh", bootstyle="secondary-outline",
                  command=lambda: self.sample_image_var.set("")).pack(side="left", padx=(8, 0))

        tb.Label(self, text="Tên job", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.name_var = tb.StringVar(value=initial["name"] if editing else "")
        tb.Entry(self, textvariable=self.name_var).pack(fill="x", padx=16)

        tb.Label(self, text="Backend gen ảnh", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.backend_var = tb.StringVar(
            value="Muse2API" if (editing and initial.get("backend") == "muse") else "Codex CLI"
        )
        tb.Combobox(self, textvariable=self.backend_var, state="readonly",
                    values=["Codex CLI", "Muse2API"]).pack(fill="x", padx=16)

        tb.Label(self, text="Thư mục lưu ảnh kết quả (bỏ trống = dùng thư mục mặc định của tool)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        out_row = tb.Frame(self)
        out_row.pack(fill="x", padx=16)
        self.output_dir_var = tb.StringVar(
            value=str(initial["output_dir"]) if editing and initial.get("output_dir") else ""
        )
        tb.Entry(out_row, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(out_row, text="📁 Chọn...", bootstyle="secondary",
                  command=self.pick_output_dir).pack(side="left", padx=(8, 0))
        tb.Button(out_row, text="✕ Mặc định", bootstyle="secondary-outline",
                  command=lambda: self.output_dir_var.set("")).pack(side="left", padx=(8, 0))

        tb.Label(self, text="Số lượng ảnh muốn tạo", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", **pad
        )
        self.count_var = tb.StringVar(value=str(initial.get("count", 1)) if editing else "1")
        tb.Entry(self, textvariable=self.count_var, width=10).pack(anchor="w", padx=16)

        tb.Label(self, text="Prompt - mô tả ảnh muốn tạo",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        text_frame = tb.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=16)
        self.prompt_text = tb.Text(text_frame, height=9, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.pack(side="left", fill="both", expand=True)
        if editing:
            self.prompt_text.insert("1.0", initial["prompt"])
        sb = tb.Scrollbar(text_frame, command=self.prompt_text.yview)
        sb.pack(side="right", fill="y")
        self.prompt_text.configure(yscrollcommand=sb.set)

        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _on_return(self, event):
        if isinstance(event.widget, tb.Text):
            return
        self.confirm()

    def load_prompt_file(self):
        f = filedialog.askopenfilename(title="Chọn file prompt .txt", filetypes=[("Text files", "*.txt")])
        if f:
            try:
                content = Path(f).read_text(encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("Lỗi", f"Không đọc được file: {exc}")
                return
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", content)

    def pick_output_dir(self):
        folder = filedialog.askdirectory(title="Chọn thư mục lưu ảnh kết quả cho job này")
        if folder:
            self.output_dir_var.set(folder)

    def pick_sample_image(self):
        f = filedialog.askopenfilename(
            title="Chọn ảnh mẫu đính kèm (tuỳ chọn)",
            filetypes=[("Ảnh", "*.png *.jpg *.jpeg *.webp"), ("Tất cả file", "*.*")],
        )
        if f:
            self.sample_image_var.set(f)

    def confirm(self):
        name = self.name_var.get().strip()
        prompt = self.prompt_text.get("1.0", "end").strip()
        count_raw = self.count_var.get().strip()

        if not name:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập tên job.")
            return
        if not prompt:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập prompt hoặc nạp từ file .txt.")
            return
        try:
            count = max(1, int(count_raw))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số lượng ảnh phải là 1 số nguyên, ví dụ 1, 2, 3.")
            return

        backend = "muse" if self.backend_var.get() == "Muse2API" else "codex"
        self.result = {"mode": "text", "name": name, "prompt": prompt, "count": count, "backend": backend}
        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            self.result["output_dir"] = Path(output_dir)
        sample_image = self.sample_image_var.get().strip()
        if sample_image:
            self.result["sample_image"] = Path(sample_image)
        self.destroy()


class ImportExcelJobsDialog(tb.Toplevel):
    """Import file Excel (cột 'name' + 'prompt') -> mỗi dòng thành 1 job chỉ-prompt."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Import job từ file Excel")
        self.geometry("640x560")
        self.minsize(560, 460)
        self.resizable(True, True)
        self.result = None  # list[dict]
        self.rows: list[dict] = []

        pad = {"padx": 16, "pady": (14, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="Huỷ", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="✓ Thêm tất cả job", bootstyle="success",
                  command=self.confirm).pack(side="right", padx=8)

        tb.Label(
            self,
            text="Chọn file Excel (.xlsx) có 1 cột 'name' (tên job) và 1 cột 'prompt'.\n"
                 "Mỗi dòng sẽ tạo 1 job chỉ-prompt (không cần ảnh mẫu), ảnh kết quả đặt tên\n"
                 "theo đúng giá trị cột 'name'.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        file_row = tb.Frame(self)
        file_row.pack(fill="x", padx=16)
        self.file_var = tb.StringVar()
        tb.Entry(file_row, textvariable=self.file_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(file_row, text="📄 Chọn file .xlsx", bootstyle="secondary",
                  command=self.pick_file).pack(side="left", padx=(8, 0))

        self.scan_hint = tb.Label(self, text="", bootstyle="secondary")
        self.scan_hint.pack(anchor="w", padx=16, pady=(6, 0))

        list_frame = tb.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        self.list_box = tb.Treeview(list_frame, columns=("name", "prompt"), show="headings",
                                     height=12, bootstyle="primary")
        self.list_box.heading("name", text="Tên job")
        self.list_box.heading("prompt", text="Prompt (rút gọn)")
        self.list_box.column("name", width=160)
        self.list_box.column("prompt", width=420)
        self.list_box.pack(fill="both", expand=True, side="left")
        sb = tb.Scrollbar(list_frame, command=self.list_box.yview)
        sb.pack(side="right", fill="y")
        self.list_box.configure(yscrollcommand=sb.set)

        count_row = tb.Frame(self)
        count_row.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(count_row, text="Số lượng ảnh muốn tạo cho MỖI dòng:").pack(side="left")
        self.count_var = tb.StringVar(value="1")
        tb.Entry(count_row, textvariable=self.count_var, width=6).pack(side="left", padx=8)
        tb.Label(count_row, text="Backend:").pack(side="left", padx=(16, 0))
        self.backend_var = tb.StringVar(value="Codex CLI")
        tb.Combobox(count_row, textvariable=self.backend_var, state="readonly", width=12,
                    values=["Codex CLI", "Muse2API"]).pack(side="left", padx=8)

        tb.Label(self, text="Tên thư mục kết quả chung (TẤT CẢ ảnh sẽ gom vào <thư mục lưu ảnh>\\<tên này>\\, "
                             "đặt tên theo từng dòng, không tạo thư mục lẻ cho mỗi dòng)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(10, 4))
        self.output_group_var = tb.StringVar()
        tb.Entry(self, textvariable=self.output_group_var).pack(fill="x", padx=16, pady=(0, 10))

        tb.Label(self, text="Thư mục lưu ảnh (bỏ trống = dùng thư mục mặc định của tool)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(0, 4))
        out_row = tb.Frame(self)
        out_row.pack(fill="x", padx=16, pady=(0, 10))
        self.output_dir_var = tb.StringVar()
        tb.Entry(out_row, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(out_row, text="📁 Chọn...", bootstyle="secondary",
                  command=self.pick_output_dir).pack(side="left", padx=(8, 0))
        tb.Button(out_row, text="✕ Mặc định", bootstyle="secondary-outline",
                  command=lambda: self.output_dir_var.set("")).pack(side="left", padx=(8, 0))

        self.bind("<Return>", lambda e: self.confirm())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def pick_file(self):
        f = filedialog.askopenfilename(
            title="Chọn file Excel", filetypes=[("Excel files", "*.xlsx")]
        )
        if not f:
            return
        self.file_var.set(f)
        if not self.output_group_var.get().strip():
            self.output_group_var.set(Path(f).stem)
        self.list_box.delete(*self.list_box.get_children())
        self.rows = []
        try:
            self.rows = core.read_excel_jobs(Path(f))
        except Exception as exc:
            self.scan_hint.configure(text=f"Lỗi đọc file: {exc}", bootstyle="danger")
            return

        for row in self.rows:
            preview = row["prompt"].replace("\n", " ")[:80]
            self.list_box.insert("", "end", values=(row["name"], preview))

        if not self.rows:
            self.scan_hint.configure(
                text="Không tìm thấy dòng nào hợp lệ (thiếu name hoặc prompt).", bootstyle="danger"
            )
        else:
            self.scan_hint.configure(text=f"Tìm thấy {len(self.rows)} dòng hợp lệ.", bootstyle="success")

    def pick_output_dir(self):
        folder = filedialog.askdirectory(title="Chọn thư mục lưu ảnh kết quả")
        if folder:
            self.output_dir_var.set(folder)

    def confirm(self):
        if not self.rows:
            messagebox.showwarning("Thiếu dữ liệu", "Hãy chọn file Excel có ít nhất 1 dòng hợp lệ.")
            return
        try:
            count = max(1, int(self.count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số lượng ảnh phải là 1 số nguyên, ví dụ 1, 2, 3.")
            return
        output_group = self.output_group_var.get().strip()
        if not output_group:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập tên thư mục kết quả chung.")
            return

        backend = "muse" if self.backend_var.get() == "Muse2API" else "codex"
        output_dir = self.output_dir_var.get().strip()
        self.result = [
            {
                "mode": "text", "name": row["name"], "prompt": row["prompt"], "count": count,
                "output_group": output_group, "backend": backend,
                **({"output_dir": Path(output_dir)} if output_dir else {}),
            }
            for row in self.rows
        ]
        self.destroy()


class BulkAddJobDialog(tb.Toplevel):
    """Chọn 1 thư mục cha, mỗi thư mục con bên trong = 1 job, dùng chung 1 prompt."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Thêm nhiều job cùng lúc (chung prompt)")
        self.geometry("700x780")
        self.minsize(600, 560)
        self.resizable(True, True)
        self.result = None  # list[dict]
        self.valid_subfolders: list[Path] = []

        section_pad = {"padx": 16, "pady": (12, 0)}

        # --- Hàng nút dưới cùng ---
        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="📄 Nạp từ file .txt", bootstyle="secondary-outline",
                  command=self.load_prompt_file).pack(side="left")
        tb.Button(btn_row, text="Huỷ", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="✓ Thêm tất cả job", bootstyle="success",
                  command=self.confirm).pack(side="right", padx=8)
        tb.Separator(self).pack(side="bottom", fill="x", padx=16, pady=(0, 10))

        # --- Khối 1: nguồn ảnh ---
        source_card = tb.Labelframe(self, text="  1. Nguồn ảnh  ", bootstyle="primary")
        source_card.pack(fill="x", **section_pad)

        tb.Label(source_card, text="Thư mục cha (mỗi thư mục con = 1 bộ ảnh biến thể)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        folder_row = tb.Frame(source_card)
        folder_row.pack(fill="x", padx=14)
        self.folder_var = tb.StringVar()
        tb.Entry(folder_row, textvariable=self.folder_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(folder_row, text="📁 Chọn thư mục cha", bootstyle="secondary",
                  command=self.pick_parent_folder).pack(side="left", padx=(8, 0))

        self.scan_hint = tb.Label(source_card, text="", bootstyle="secondary")
        self.scan_hint.pack(anchor="w", padx=14, pady=(6, 0))

        list_frame = tb.Frame(source_card)
        list_frame.pack(fill="both", expand=False, padx=14, pady=(6, 0))
        self.list_box = tb.Treeview(list_frame, columns=("check", "name"), show="headings", height=8,
                                     bootstyle="primary")
        self.list_box.heading("check", text="Chọn?")
        self.list_box.heading("name", text="Biến thể (nhấp đúp để bỏ/chọn)")
        self.list_box.column("check", width=60, anchor="center")
        self.list_box.column("name", width=480)
        self.list_box.pack(fill="both", expand=True, side="left")
        sb_list = tb.Scrollbar(list_frame, command=self.list_box.yview)
        sb_list.pack(side="right", fill="y")
        self.list_box.configure(yscrollcommand=sb_list.set)
        self.list_box.bind("<Double-1>", self.toggle_row)

        select_row = tb.Frame(source_card)
        select_row.pack(fill="x", padx=14, pady=(8, 12))
        tb.Button(select_row, text="✅ Chọn tất cả", bootstyle="info-outline",
                  command=self.select_all).pack(side="left")
        tb.Button(select_row, text="❌ Bỏ chọn tất cả", bootstyle="info-outline",
                  command=self.deselect_all).pack(side="left", padx=8)
        tb.Button(select_row, text="🗑 Xoá khỏi danh sách", bootstyle="danger-outline",
                  command=self.remove_rows).pack(side="left", padx=8)

        # --- Khối 2: đầu ra ---
        output_card = tb.Labelframe(self, text="  2. Đầu ra  ", bootstyle="primary")
        output_card.pack(fill="x", **section_pad)

        tb.Label(output_card, text="Tên thư mục kết quả chung (TẤT CẢ ảnh gom vào "
                                    "<thư mục lưu ảnh>\\<tên này>\\, đặt tên theo từng biến thể)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        self.output_group_var = tb.StringVar()
        tb.Entry(output_card, textvariable=self.output_group_var).pack(fill="x", padx=14)

        tb.Label(output_card, text="Thư mục lưu ảnh (bỏ trống = dùng thư mục mặc định của tool)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        out_row = tb.Frame(output_card)
        out_row.pack(fill="x", padx=14)
        self.output_dir_var = tb.StringVar()
        tb.Entry(out_row, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tb.Button(out_row, text="📁 Chọn...", bootstyle="secondary",
                  command=self.pick_output_dir).pack(side="left", padx=(8, 0))
        tb.Button(out_row, text="✕ Mặc định", bootstyle="secondary-outline",
                  command=lambda: self.output_dir_var.set("")).pack(side="left", padx=(8, 0))

        backend_row = tb.Frame(output_card)
        backend_row.pack(fill="x", padx=14, pady=(10, 12))
        tb.Label(backend_row, text="Backend gen ảnh:", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.backend_var = tb.StringVar(value="Codex CLI")
        tb.Combobox(backend_row, textvariable=self.backend_var, state="readonly", width=12,
                    values=["Codex CLI", "Muse2API"]).pack(side="left", padx=8)

        # --- Khối 3: prompt ---
        prompt_card = tb.Labelframe(self, text="  3. Prompt dùng chung  ", bootstyle="primary")
        prompt_card.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        text_frame = tb.Frame(prompt_card)
        text_frame.pack(fill="both", expand=True, padx=14, pady=(10, 12))
        self.prompt_text = tb.Text(text_frame, height=8, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.pack(side="left", fill="both", expand=True)
        sb = tb.Scrollbar(text_frame, command=self.prompt_text.yview)
        sb.pack(side="right", fill="y")
        self.prompt_text.configure(yscrollcommand=sb.set)

        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _on_return(self, event):
        if isinstance(event.widget, tb.Text):
            return
        self.confirm()

    def pick_parent_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục cha")
        if not folder:
            return
        self.folder_var.set(folder)
        if not self.output_group_var.get().strip():
            self.output_group_var.set(Path(folder).name)
        self.list_box.delete(*self.list_box.get_children())
        self.valid_subfolders = []

        skipped = 0
        for sub in sorted(Path(folder).iterdir()):
            if not sub.is_dir():
                continue
            try:
                core.collect_job_images(sub)
            except Exception:
                skipped += 1
                continue
            self.valid_subfolders.append(sub)
            self.list_box.insert("", "end", values=("✅", sub.name))

        if not self.valid_subfolders:
            self.scan_hint.configure(
                text="Không tìm thấy thư mục con nào có ảnh.", bootstyle="danger"
            )
        else:
            msg = f"Tìm thấy {len(self.valid_subfolders)} thư mục hợp lệ (đang chọn hết)."
            if skipped:
                msg += f" (bỏ qua {skipped} thư mục không có ảnh nào)"
            self.scan_hint.configure(text=msg, bootstyle="success")

    def toggle_row(self, event):
        item = self.list_box.identify_row(event.y)
        if not item:
            return
        current = self.list_box.set(item, "check")
        self.list_box.set(item, "check", "⬜" if current == "✅" else "✅")

    def select_all(self):
        for item in self.list_box.get_children():
            self.list_box.set(item, "check", "✅")

    def deselect_all(self):
        for item in self.list_box.get_children():
            self.list_box.set(item, "check", "⬜")

    def remove_rows(self):
        sel = self.list_box.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Hãy bôi đen (chọn) 1 hoặc nhiều dòng trong bảng trước.")
            return
        indices = sorted((self.list_box.index(item) for item in sel), reverse=True)
        for item in sel:
            self.list_box.delete(item)
        for idx in indices:
            del self.valid_subfolders[idx]
        self.scan_hint.configure(
            text=f"Còn {len(self.valid_subfolders)} thư mục trong danh sách.", bootstyle="success"
        )

    def load_prompt_file(self):
        f = filedialog.askopenfilename(title="Chọn file prompt .txt", filetypes=[("Text files", "*.txt")])
        if f:
            try:
                content = Path(f).read_text(encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("Lỗi", f"Không đọc được file: {exc}")
                return
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", content)

    def pick_output_dir(self):
        folder = filedialog.askdirectory(title="Chọn thư mục lưu ảnh kết quả")
        if folder:
            self.output_dir_var.set(folder)

    def confirm(self):
        prompt = self.prompt_text.get("1.0", "end").strip()
        output_group = self.output_group_var.get().strip()
        if not self.valid_subfolders:
            messagebox.showwarning("Thiếu thông tin", "Hãy chọn thư mục cha có ít nhất 1 thư mục con hợp lệ.")
            return
        if not prompt:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập prompt hoặc nạp từ file .txt.")
            return
        if not output_group:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập tên thư mục kết quả chung.")
            return

        checked = [
            self.valid_subfolders[self.list_box.index(item)]
            for item in self.list_box.get_children()
            if self.list_box.set(item, "check") == "✅"
        ]
        if not checked:
            messagebox.showwarning("Chưa chọn biến thể nào", "Hãy tick ✅ ít nhất 1 biến thể để tạo job.")
            return

        backend = "muse" if self.backend_var.get() == "Muse2API" else "codex"
        output_dir = self.output_dir_var.get().strip()
        self.result = [
            {
                "mode": "image", "name": sub.name, "folder": sub, "prompt": prompt,
                "output_group": output_group, "backend": backend,
                **({"output_dir": Path(output_dir)} if output_dir else {}),
            }
            for sub in checked
        ]
        self.destroy()


class AccountsDialog(tb.Toplevel):
    """Quản lý danh sách tài khoản ChatGPT (mỗi tài khoản = 1 thư mục CODEX_HOME riêng)."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Quản lý tài khoản ChatGPT")
        self.geometry("680x520")
        self.minsize(520, 420)
        self.resizable(True, True)

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)
        btn_row_1 = tb.Frame(btn_row)
        btn_row_1.pack(fill="x")
        btn_row_2 = tb.Frame(btn_row)
        btn_row_2.pack(fill="x", pady=(6, 0))

        tb.Button(btn_row_1, text="➕ Thêm tài khoản mới", bootstyle="success",
                  command=self.add_account).pack(side="left")
        tb.Button(btn_row_1, text="🔘 Bật/Tắt đã chọn", bootstyle="warning",
                  command=self.toggle_selected).pack(side="left", padx=8)
        tb.Button(btn_row_1, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")

        tb.Button(btn_row_2, text="🔑 Đăng nhập tài khoản đã chọn", bootstyle="info",
                  command=self.login_selected).pack(side="left")
        tb.Button(btn_row_2, text="🗑 Xoá khỏi danh sách", bootstyle="danger-outline",
                  command=self.remove_selected).pack(side="left", padx=8)

        tb.Label(
            self,
            text="Mỗi tài khoản = 1 thư mục riêng chứa thông tin đăng nhập (CODEX_HOME).\n"
                 "Khi chạy, task sẽ chia round-robin cho các tài khoản đang BẬT (✅) dưới đây.\n"
                 "Tài khoản TẮT (⬜) vẫn được giữ trong danh sách, chỉ không dùng khi gen ảnh.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        list_frame = tb.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=16)
        self.listbox = tb.Treeview(list_frame, columns=("active", "path"), show="headings",
                                    height=10, bootstyle="primary")
        self.listbox.heading("active", text="Bật?")
        self.listbox.heading("path", text="Thư mục tài khoản (CODEX_HOME)")
        self.listbox.column("active", width=60, anchor="center")
        self.listbox.column("path", width=500)
        self.listbox.pack(fill="both", expand=True, side="left")
        sb = tb.Scrollbar(list_frame, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<Double-1>", lambda e: self.toggle_selected())

        hint = tb.Label(self, text="(Nhấp đúp vào 1 dòng để bật/tắt nhanh)", bootstyle="secondary")
        hint.pack(anchor="w", padx=16, pady=(4, 0))

        self._reload()
        self.grab_set()

    def _reload(self):
        self.listbox.delete(*self.listbox.get_children())
        for path, active in core.read_account_entries(BASE_DIR):
            self.listbox.insert("", "end", values=("✅ Bật" if active else "⬜ Tắt", str(path)))

    def _save(self, entries: list[tuple[Path, bool]]):
        core.write_account_entries(BASE_DIR, entries)
        self._reload()

    def _selected_path(self) -> Path | None:
        sel = self.listbox.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Hãy chọn 1 tài khoản trong danh sách trước.")
            return None
        return Path(self.listbox.item(sel[0], "values")[1])

    def add_account(self):
        folder = filedialog.askdirectory(
            title="Chọn (hoặc tạo) thư mục riêng cho tài khoản này, ví dụ C:\\codex-accounts\\acc2"
        )
        if not folder:
            return
        entries = core.read_account_entries(BASE_DIR)
        new_path = Path(folder)
        if any(p == new_path for p, _ in entries):
            messagebox.showinfo("Đã có", "Thư mục này đã có trong danh sách tài khoản.")
            return
        entries.append((new_path, True))
        self._save(entries)
        if messagebox.askyesno(
            "Đăng nhập ngay?",
            f"Đã thêm tài khoản (đang BẬT):\n{new_path}\n\n"
            "Mở cửa sổ đăng nhập ChatGPT cho tài khoản này ngay bây giờ?",
        ):
            self._login(new_path)

    def toggle_selected(self):
        path = self._selected_path()
        if path is None:
            return
        entries = core.read_account_entries(BASE_DIR)
        entries = [(p, (not active) if p == path else active) for p, active in entries]
        self._save(entries)

    def login_selected(self):
        path = self._selected_path()
        if path is None:
            return
        self._login(path)

    def _login(self, codex_home: Path):
        codex_bin = core.find_codex_bin()
        if not codex_bin:
            messagebox.showerror("Thiếu Codex CLI", "Không tìm thấy lệnh 'codex' trong PATH.")
            return
        codex_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        try:
            # Mở cửa sổ console riêng (CẦN hiện ra) để người dùng đăng nhập ChatGPT
            # (trình duyệt sẽ tự mở để chọn tài khoản).
            subprocess.Popen(
                [codex_bin, "login"],
                env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
            messagebox.showinfo(
                "Đang đăng nhập",
                f"Đã mở cửa sổ đăng nhập cho:\n{codex_home}\n\n"
                "Hãy hoàn tất đăng nhập ChatGPT trong cửa sổ/trình duyệt vừa mở, "
                "rồi quay lại dùng tool bình thường.",
            )
        except Exception as exc:
            messagebox.showerror("Lỗi", f"Không mở được cửa sổ đăng nhập: {exc}")

    def remove_selected(self):
        path = self._selected_path()
        if path is None:
            return
        entries = [(p, active) for p, active in core.read_account_entries(BASE_DIR) if p != path]
        self._save(entries)


class MuseAccountsDialog(tb.Toplevel):
    """Quản lý tài khoản Muse - các tài khoản này do MUSE2API quản lý nội bộ (cookie đăng
    nhập muse.ai), KHÔNG phải tài khoản ChatGPT CLI. Dialog này gọi API admin của
    MUSE2API (cần Base URL + API Key đã cấu hình) để xem/bật/tắt từng tài khoản."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Quản lý tài khoản Muse")
        self.geometry("760x520")
        self.minsize(640, 420)
        self.resizable(True, True)
        self.accounts: list[dict] = []

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)
        tb.Button(btn_row, text="🔄 Làm mới", bootstyle="secondary",
                  command=self.reload).pack(side="left")
        tb.Button(btn_row, text="🔘 Bật/Tắt đã chọn", bootstyle="warning",
                  command=self.toggle_selected).pack(side="left", padx=8)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")

        tb.Label(
            self,
            text="Danh sách tài khoản Muse do MUSE2API quản lý nội bộ (không phải tài khoản "
                 "ChatGPT CLI).\nTài khoản TẮT sẽ không được MUSE2API dùng để gen ảnh. Cột "
                 "\"Trạng thái\" báo tài khoản\nnào có vẻ đã hết limit/lỗi (⚠️) - muốn thêm tài "
                 "khoản Muse mới thì vào trang quản trị MUSE2API.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        self.status_var = tb.StringVar(value="Đang tải...")
        tb.Label(self, textvariable=self.status_var, bootstyle="secondary").pack(anchor="w", padx=16)

        list_frame = tb.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        self.listbox = tb.Treeview(
            list_frame, columns=("enabled", "label", "status", "quota", "used"),
            show="headings", height=10, bootstyle="primary",
        )
        self.listbox.heading("enabled", text="Bật?")
        self.listbox.heading("label", text="Tên tài khoản")
        self.listbox.heading("status", text="Trạng thái")
        self.listbox.heading("quota", text="Hạn mức tuần")
        self.listbox.heading("used", text="Đã dùng (lần)")
        self.listbox.column("enabled", width=60, anchor="center")
        self.listbox.column("label", width=150)
        self.listbox.column("status", width=270)
        self.listbox.column("quota", width=100, anchor="center")
        self.listbox.column("used", width=90, anchor="center")
        self.listbox.pack(fill="both", expand=True, side="left")
        sb = tb.Scrollbar(list_frame, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<Double-1>", lambda e: self.toggle_selected())

        hint = tb.Label(self, text="(Nhấp đúp vào 1 dòng để bật/tắt nhanh)", bootstyle="secondary")
        hint.pack(anchor="w", padx=16, pady=(4, 0))

        self.grab_set()
        self.reload()

    def _muse_config(self) -> dict | None:
        cfg = core.load_muse_config(BASE_DIR)
        if not cfg:
            messagebox.showwarning(
                "Chưa cấu hình MUSE2API",
                "Hãy bấm \"🅜 Cấu hình MUSE2API\" để thiết lập Base URL / API Key trước.",
            )
            return None
        return cfg

    def reload(self):
        cfg = self._muse_config()
        if not cfg:
            self.status_var.set("Chưa cấu hình MUSE2API.")
            return
        self.status_var.set("Đang tải danh sách tài khoản...")

        def work():
            try:
                accounts = core.list_muse_accounts(cfg["base_url"], cfg["api_key"])
                self.after(0, lambda: self._on_loaded(accounts))
            except Exception as exc:
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, accounts: list[dict]):
        self.accounts = accounts
        self.listbox.delete(*self.listbox.get_children())
        limited_count = 0
        for acc in accounts:
            limited = core.muse_account_is_limited(acc)
            if limited:
                limited_count += 1
            quota = acc.get("quota") or {}
            quota_text = f"{quota.get('weekly_used_pct', '?')}%" if quota else "?"
            note = core.translate_muse_error(acc.get("note") or ("OK" if acc.get("ok") else "?"))
            status_text = ("⚠️ " if limited else "✅ ") + note
            self.listbox.insert("", "end", iid=acc["id"], values=(
                "✅ Bật" if acc.get("enabled", True) else "⬜ Tắt",
                core.muse_account_label(acc),
                status_text,
                quota_text,
                acc.get("use_count", 0),
            ))
        total = len(accounts)
        if not accounts:
            self.status_var.set("Không có tài khoản Muse nào (hãy thêm trong trang quản trị MUSE2API).")
        elif limited_count:
            self.status_var.set(f"Có {total} tài khoản, {limited_count} tài khoản có vẻ đã hết limit/lỗi (⚠️).")
        else:
            self.status_var.set(f"Có {total} tài khoản, tất cả đều ổn.")

    def _on_error(self, exc: Exception):
        self.status_var.set(f"LỖI khi tải danh sách tài khoản Muse: {exc}")

    def _selected_id(self) -> str | None:
        sel = self.listbox.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Hãy chọn 1 tài khoản trong danh sách trước.")
            return None
        return sel[0]

    def toggle_selected(self):
        aid = self._selected_id()
        if aid is None:
            return
        acc = next((a for a in self.accounts if a["id"] == aid), None)
        if acc is None:
            return
        cfg = self._muse_config()
        if not cfg:
            return
        new_enabled = not acc.get("enabled", True)
        try:
            core.set_muse_account_enabled(cfg["base_url"], cfg["api_key"], aid, new_enabled)
        except Exception as exc:
            messagebox.showerror("Lỗi", f"Không bật/tắt được tài khoản Muse: {exc}")
            return
        self.reload()


class MuseConfigDialog(tb.Toplevel):
    """Cấu hình kết nối tới server MUSE2API tự host (Base URL + API Key)."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Cấu hình MUSE2API")
        self.geometry("560x320")
        self.minsize(480, 280)
        self.resizable(True, True)
        self.saved = False

        pad = {"padx": 16, "pady": (14, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="💾 Lưu", bootstyle="success",
                  command=self.save).pack(side="right", padx=8)
        tb.Button(btn_row, text="🔌 Kiểm tra kết nối", bootstyle="info-outline",
                  command=self.test_connection).pack(side="left")

        tb.Label(
            self,
            text="MUSE2API là server tự host (Docker/Python) biến muse.ai thành API ảnh/video.\n"
                 "Cần tự triển khai server đó trước, nhập đúng URL + API Key vào đây.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        existing = core.load_muse_config(BASE_DIR) or {}

        tb.Label(self, text="Base URL (ví dụ: http://127.0.0.1:18610)",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.url_var = tb.StringVar(value=existing.get("base_url", ""))
        tb.Entry(self, textvariable=self.url_var).pack(fill="x", padx=16)

        tb.Label(self, text="MUSE2API_KEY", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.key_var = tb.StringVar(value=existing.get("api_key", ""))
        tb.Entry(self, textvariable=self.key_var, show="•").pack(fill="x", padx=16)

        self.status_label = tb.Label(self, text="", bootstyle="secondary", justify="left", wraplength=500)
        self.status_label.pack(anchor="w", padx=16, pady=(10, 0))

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def test_connection(self):
        base_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        if not base_url or not api_key:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập đủ Base URL và API Key trước khi kiểm tra.")
            return
        self.status_label.configure(text="Đang kiểm tra (gửi 1 tin nhắn chat ngắn)...", bootstyle="secondary")
        self.update()
        ok, msg = core.test_muse_connection(base_url, api_key)
        self.status_label.configure(text=msg, bootstyle="success" if ok else "danger")

    def save(self):
        base_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        if not base_url or not api_key:
            messagebox.showwarning("Thiếu thông tin", "Hãy nhập đủ Base URL và API Key.")
            return
        core.save_muse_config(BASE_DIR, base_url, api_key)
        self.saved = True
        messagebox.showinfo("Đã lưu", "Đã lưu cấu hình MUSE2API.")


class VideoMakerDialog(tb.Toplevel):
    """Tạo hàng loạt video demo (ảnh kết quả -> khung upload -> ảnh kết quả) từ 2 thư mục."""

    def __init__(self, master):
        super().__init__(master)
        self.title("🎬 Tạo video Kiểu 1 (hàng loạt)")
        self.geometry("760x640")
        self.minsize(640, 540)
        self.resizable(True, True)
        self.running = False
        self.log_queue = queue.Queue()

        pad = {"padx": 16, "pady": (12, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        self.run_btn = tb.Button(btn_row, text="▶ Bắt đầu tạo video", bootstyle="success",
                                  command=self.start)
        self.run_btn.pack(side="right", padx=8)

        existing = core.load_video_config(BASE_DIR)

        tb.Label(
            self,
            text="Nguyên liệu cần có:\n"
                 "  1) Thư mục ẢNH GỐC (ảnh user tải lên) - mỗi file = 1 video sẽ được tạo.\n"
                 "  2) Thư mục ẢNH KẾT QUẢ - mỗi ảnh kết quả phải TRÙNG TÊN FILE với ảnh gốc tương ứng.\n"
                 "  3) (Tuỳ chọn) Video logo bay có kênh alpha (.mov) - chạy lặp xuyên suốt video.\n"
                 "  4) (Tuỳ chọn) Video outtro (.mp4) - nối thêm vào cuối mỗi video.\n"
                 "  5) (Tuỳ chọn) File nhạc nền - tự lặp/cắt cho vừa video, trộn chung với audio outtro.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        def folder_row(label_text, initial, pick_title):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📁 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(filedialog.askdirectory(title=pick_title) or var.get())
                      ).pack(side="left", padx=(8, 0))
            return var

        def file_row(label_text, initial, pick_title, filetypes):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📄 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(
                          filedialog.askopenfilename(title=pick_title, filetypes=filetypes) or var.get()
                      )).pack(side="left", padx=(8, 0))
            tb.Button(row, text="✕ Bỏ", bootstyle="secondary-outline",
                      command=lambda: var.set("")).pack(side="left", padx=(8, 0))
            return var

        self.originals_var = folder_row(
            "1) Thư mục ảnh gốc (ảnh user tải lên)", existing.get("originals_dir"),
            "Chọn thư mục ảnh gốc",
        )
        self.results_var = folder_row(
            "2) Thư mục ảnh kết quả (tên file trùng với ảnh gốc)", existing.get("results_dir"),
            "Chọn thư mục ảnh kết quả",
        )
        self.out_var = folder_row(
            "3) Thư mục lưu video đầu ra", existing.get("video_out_dir"),
            "Chọn thư mục lưu video",
        )
        default_logo = str(DEFAULT_LOGO_PATH) if DEFAULT_LOGO_PATH.exists() else ""
        default_outtro = str(DEFAULT_OUTTRO_PATH) if DEFAULT_OUTTRO_PATH.exists() else ""

        self.logo_var = file_row(
            "4) Video logo bay (.mov, có alpha) - tuỳ chọn (mặc định có sẵn)",
            existing.get("logo_path") or default_logo,
            "Chọn video logo bay", [("Video", "*.mov *.mp4"), ("Tất cả", "*.*")],
        )
        self.outtro_var = file_row(
            "5) Video outtro - tuỳ chọn (mặc định có sẵn)",
            existing.get("outtro_path") or default_outtro,
            "Chọn video outtro", [("Video", "*.mp4 *.mov"), ("Tất cả", "*.*")],
        )
        self.audio_var = file_row(
            "6) Nhạc nền - tuỳ chọn (tự lặp/cắt vừa video, trộn chung với audio outtro nếu có)",
            existing.get("audio_path"),
            "Chọn file nhạc nền", [("Audio", "*.mp3 *.m4a *.wav *.aac"), ("Tất cả", "*.*")],
        )
        self.upload_frame_var = file_row(
            "7) Ảnh khung upload tuỳ chỉnh - tuỳ chọn (mặc định dùng khung có sẵn của tool;\n"
            "   nền ảnh phải màu đen, vùng khung chứa ảnh là mảng màu/sáng nổi bật - tool sẽ tự dò)",
            existing.get("upload_frame_path"),
            "Chọn ảnh khung upload", [("Ảnh", "*.png *.jpg *.jpeg"), ("Tất cả", "*.*")],
        )

        count_row = tb.Frame(self)
        count_row.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(count_row, text="Số ảnh kết quả / video:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.result_count_var = tb.StringVar(value=str(existing.get("result_count", 2)))
        result_count_entry = tb.Entry(count_row, textvariable=self.result_count_var, width=6)
        result_count_entry.pack(side="left", padx=8)
        result_count_entry.bind("<FocusOut>", lambda e: self._rebuild_transition_pickers())
        result_count_entry.bind("<Return>", lambda e: self._rebuild_transition_pickers())

        tb.Label(count_row, text="Số ảnh hiện TRƯỚC khung upload:",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.before_count_var = tb.StringVar(
            value=str(existing.get("before_count") or existing.get("result_count", 2))
        )
        tb.Entry(count_row, textvariable=self.before_count_var, width=6).pack(side="left", padx=8)
        tb.Label(
            self,
            text="(Đoạn sau khung upload luôn hiện lại ĐỦ số ảnh kết quả; số ảnh trước khung\n"
                 "upload nhỏ hơn tổng số ảnh sẽ làm khung upload xuất hiện SỚM hơn.)",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(2, 0))

        transition_header = tb.Frame(self)
        transition_header.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(transition_header, text="Hiệu ứng chuyển cảnh giữa TỪNG CẶP ảnh kết quả:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Button(transition_header, text="🔄 Cập nhật theo số ảnh", bootstyle="secondary-outline",
                  command=self._rebuild_transition_pickers).pack(side="left", padx=8)

        self._transition_label_for_value = {v: k for k, v in TRANSITION_OPTIONS.items()}
        saved_transitions = existing.get("transitions")
        if not saved_transitions and existing.get("transition"):
            saved_transitions = [existing["transition"]]
        self._saved_transition_labels = [
            self._transition_label_for_value.get(t, next(iter(TRANSITION_OPTIONS)))
            for t in (saved_transitions or [])
        ]
        self.transition_picker_frame = tb.Frame(self)
        self.transition_picker_frame.pack(fill="x", padx=16, pady=(4, 0))
        self.transition_vars: list[tb.StringVar] = []
        self._rebuild_transition_pickers()

        workers_row = tb.Frame(self)
        workers_row.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(workers_row, text="Số video chạy song song:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.workers_var = tb.StringVar(value=str(existing.get("workers", 2)))
        tb.Entry(workers_row, textvariable=self.workers_var, width=6).pack(side="left", padx=8)

        tb.Label(workers_row, text="Âm lượng nhạc nền (0-1):",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.audio_volume_var = tb.StringVar(value=str(existing.get("audio_volume", 0.5)))
        tb.Entry(workers_row, textvariable=self.audio_volume_var, width=6).pack(side="left", padx=8)

        self.status_var = tb.StringVar(value="Sẵn sàng.")
        tb.Label(self, textvariable=self.status_var, bootstyle="secondary").pack(
            anchor="w", padx=16, pady=(10, 0)
        )

        log_frame = tb.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self.log_text = tb.Text(log_frame, state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                                 wrap="word", font=("Consolas", 9))
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("warn", foreground="#fbbf24")
        sb = tb.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.bind("<Escape>", lambda e: self.destroy())
        self.after(100, self._poll_log_queue)
        self.grab_set()

    def _append_log(self, text: str) -> None:
        tag = None
        low = text.lower()
        if "lỗi" in low:
            tag = "err"
        elif "cảnh báo" in low or "bỏ qua" in low:
            tag = "warn"
        elif "xong" in low:
            tag = "ok"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _rebuild_transition_pickers(self):
        """Tạo lại danh sách dropdown chọn hiệu ứng chuyển cảnh - 1 dropdown cho mỗi
        CẶP ảnh liên tiếp (ảnh 1→2, 2→3,...), theo đúng số ảnh kết quả hiện đang nhập.
        Giữ lại lựa chọn cũ nếu cặp đó vẫn còn tồn tại sau khi đổi số ảnh."""
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            result_count = 2
        n_pairs = max(1, result_count - 1)

        old_labels = [v.get() for v in self.transition_vars]
        for w in self.transition_picker_frame.winfo_children():
            w.destroy()
        self.transition_vars = []

        for i in range(n_pairs):
            if i < len(old_labels):
                default_label = old_labels[i]
            elif i < len(self._saved_transition_labels):
                default_label = self._saved_transition_labels[i]
            else:
                default_label = next(iter(TRANSITION_OPTIONS))
            row = tb.Frame(self.transition_picker_frame)
            row.pack(fill="x", pady=2)
            tb.Label(row, text=f"Ảnh {i + 1} → Ảnh {i + 2}:", width=16, anchor="w").pack(side="left")
            var = tb.StringVar(value=default_label)
            tb.Combobox(row, textvariable=var, state="readonly",
                        values=list(TRANSITION_OPTIONS.keys()), width=28).pack(side="left", padx=8)
            self.transition_vars.append(var)

    def start(self):
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tiến trình tạo video chạy, vui lòng đợi xong.")
            return
        originals_dir = self.originals_var.get().strip()
        results_dir = self.results_var.get().strip()
        out_dir = self.out_var.get().strip()
        logo_path = self.logo_var.get().strip() or None
        outtro_path = self.outtro_var.get().strip() or None
        audio_path = self.audio_var.get().strip() or None
        upload_frame_path = self.upload_frame_var.get().strip() or None

        if not originals_dir or not results_dir or not out_dir:
            messagebox.showwarning(
                "Thiếu thông tin",
                "Hãy chọn đủ: thư mục ảnh gốc, thư mục ảnh kết quả, và thư mục lưu video.",
            )
            return
        try:
            workers = max(1, int(self.workers_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số video chạy song song phải là số nguyên, ví dụ 1, 2, 3.")
            return
        try:
            audio_volume = min(1.0, max(0.0, float(self.audio_volume_var.get().strip())))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Âm lượng nhạc nền phải là số từ 0 đến 1, ví dụ 0.5.")
            return
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số ảnh kết quả / video phải là số nguyên, ví dụ 2, 3, 4.")
            return
        try:
            before_count = max(1, min(result_count, int(self.before_count_var.get().strip())))
        except ValueError:
            messagebox.showwarning(
                "Sai định dạng", "Số ảnh hiện trước khung upload phải là số nguyên, ví dụ 1, 2."
            )
            return
        self._rebuild_transition_pickers()  # đảm bảo đủ dropdown đúng theo result_count hiện tại
        transitions = [TRANSITION_OPTIONS.get(v.get(), "slideright") for v in self.transition_vars]

        core.save_video_config(
            BASE_DIR, originals_dir=originals_dir, results_dir=results_dir,
            video_out_dir=out_dir, logo_path=logo_path, outtro_path=outtro_path,
            audio_path=audio_path, audio_volume=audio_volume, workers=workers,
            result_count=result_count, before_count=before_count, transitions=transitions,
            upload_frame_path=upload_frame_path,
        )

        self.running = True
        self.run_btn.configure(state="disabled")
        self.status_var.set("Đang chạy...")
        threading.Thread(
            target=self._worker,
            args=(originals_dir, results_dir, out_dir, logo_path, outtro_path,
                  audio_path, audio_volume, result_count, before_count, transitions,
                  upload_frame_path, workers),
            daemon=True,
        ).start()

    def _worker(self, originals_dir, results_dir, out_dir, logo_path, outtro_path,
                audio_path, audio_volume, result_count, before_count, transitions,
                upload_frame_path, workers):
        def log(msg: str) -> None:
            self.log_queue.put(msg)

        try:
            outputs = video_core.auto_batch_from_folders(
                Path(originals_dir), Path(results_dir), Path(out_dir),
                logo_path=Path(logo_path) if logo_path else None,
                outtro_path=Path(outtro_path) if outtro_path else None,
                audio_path=Path(audio_path) if audio_path else None,
                audio_volume=audio_volume,
                result_count=result_count,
                before_count=before_count,
                transition=transitions,
                upload_frame_path=Path(upload_frame_path) if upload_frame_path else None,
                on_log=log,
                max_workers=workers,
            )
            log(f"===== XONG: đã tạo {len(outputs)} video =====")
            self.after(0, lambda: self.status_var.set(f"Xong: đã tạo {len(outputs)} video."))
        except Exception as exc:
            log(f"LỖI: {exc}")
            self.after(0, lambda: self.status_var.set("Lỗi, xem log bên dưới."))
        finally:
            self.running = False
            self.after(0, lambda: self.run_btn.configure(state="normal"))


class BoxPickerDialog(tb.Toplevel):
    """Cho người dùng kéo chuột khoanh 1 vùng chữ nhật trên ảnh (khung hình đầu của 1
    clip KOL) - dùng để khai báo khung ảnh sẽ fit ảnh kết quả vào khi tạo video Hook 3."""

    def __init__(self, master, image_path: Path, default_box: tuple[int, int, int, int] | None = None):
        super().__init__(master)
        self.title("Khoanh vùng khung ảnh trong clip KOL")
        self.resizable(False, False)
        self.result: tuple[int, int, int, int] | None = None
        self._box: tuple[int, int, int, int] | None = None
        self._start = None
        self._rect_id = None

        img = Image.open(image_path).convert("RGB")
        self.orig_w, self.orig_h = img.size
        max_dim = 720
        self.scale = min(max_dim / self.orig_w, max_dim / self.orig_h, 1.0)
        disp_w, disp_h = max(1, int(self.orig_w * self.scale)), max(1, int(self.orig_h * self.scale))
        self._photo = ImageTk.PhotoImage(img.resize((disp_w, disp_h), Image.LANCZOS))

        tb.Label(
            self, text="Kéo chuột để khoanh vùng khung chứa ảnh trong khung hình này "
                        "(vùng mà ảnh kết quả của bạn sẽ được chèn vào). Mặc định đã gợi ý\n"
                        "1 vùng ở góc trái màn - bạn có thể kéo lại nếu muốn đổi vị trí khác.",
            bootstyle="secondary", wraplength=disp_w,
        ).pack(anchor="w", padx=12, pady=(12, 6))

        self.canvas = tk.Canvas(self, width=disp_w, height=disp_h, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=12)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        self.box_var = tb.StringVar(value="Chưa chọn vùng nào.")
        tb.Label(self, textvariable=self.box_var, bootstyle="info").pack(anchor="w", padx=12, pady=(6, 0))

        if default_box:
            dx, dy, dw, dh = default_box
            dx = max(0, min(dx, self.orig_w - 1))
            dy = max(0, min(dy, self.orig_h - 1))
            dw = max(1, min(dw, self.orig_w - dx))
            dh = max(1, min(dh, self.orig_h - dy))
            self._box = (dx, dy, dw, dh)
            cx0, cy0 = dx * self.scale, dy * self.scale
            cx1, cy1 = (dx + dw) * self.scale, (dy + dh) * self.scale
            self._rect_id = self.canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="#22c55e", width=2)
            self.box_var.set(f"Vùng gợi ý (góc trái): x={dx}, y={dy}, rộng={dw}, cao={dh}")

        btn_row = tb.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=12)
        tb.Button(btn_row, text="Huỷ", bootstyle="secondary-outline", command=self.destroy).pack(side="right")
        tb.Button(btn_row, text="✓ Xác nhận vùng này", bootstyle="success",
                  command=self.confirm).pack(side="right", padx=8)

        self.grab_set()

    def _on_press(self, event):
        self._start = (event.x, event.y)
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#22c55e", width=2
        )

    def _on_drag(self, event):
        if self._start and self._rect_id:
            x0, y0 = self._start
            self.canvas.coords(self._rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event):
        if not self._start:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        x0, x1 = sorted((max(0, min(self._start[0], cw)), max(0, min(event.x, cw))))
        y0, y1 = sorted((max(0, min(self._start[1], ch)), max(0, min(event.y, ch))))
        ox0, oy0 = int(x0 / self.scale), int(y0 / self.scale)
        ox1, oy1 = int(x1 / self.scale), int(y1 / self.scale)
        self._box = (ox0, oy0, max(1, ox1 - ox0), max(1, oy1 - oy0))
        self.box_var.set(f"Vùng đã chọn: x={ox0}, y={oy0}, rộng={self._box[2]}, cao={self._box[3]}")

    def confirm(self):
        if not self._box or self._box[2] < 20 or self._box[3] < 20:
            messagebox.showwarning("Chưa chọn vùng", "Hãy kéo chuột khoanh 1 vùng đủ lớn trước.")
            return
        self.result = self._box
        self.destroy()


class KolTemplatesDialog(tb.Toplevel):
    """Quản lý danh sách clip KOL (mỗi clip = 1 video phản ứng/chỉ tay + khung ảnh đã
    khoanh sẵn trong clip đó) dùng cho video Hook 3. Nhiều clip khác nhau (nhân vật khác
    nhau) sẽ được CHỌN NGẪU NHIÊN 1 cái/video khi tạo hàng loạt."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Quản lý clip KOL (Hook 3)")
        self.geometry("720x460")
        self.minsize(620, 380)
        self.resizable(True, True)
        self.templates: list[dict] = core.load_kol_templates(BASE_DIR)

        tb.Label(
            self,
            text="Mỗi clip KOL = 1 video có người chỉ tay/phản ứng + 1 khung ảnh có sẵn\n"
                 "trong clip (bạn khoanh vùng 1 lần khi thêm). Khi tạo video Hook 3 hàng loạt,\n"
                 "mỗi video sẽ chọn NGẪU NHIÊN 1 clip KOL trong danh sách này.",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        list_frame = tb.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=16)
        self.listbox = tb.Treeview(
            list_frame, columns=("name", "video", "box"), show="headings", height=10, bootstyle="primary"
        )
        self.listbox.heading("name", text="Tên")
        self.listbox.heading("video", text="File video")
        self.listbox.heading("box", text="Khung ảnh (x,y,rộng,cao)")
        self.listbox.column("name", width=120)
        self.listbox.column("video", width=320)
        self.listbox.column("box", width=180)
        self.listbox.pack(fill="both", expand=True, side="left")
        sb = tb.Scrollbar(list_frame, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        btn_row = tb.Frame(self)
        btn_row.pack(fill="x", padx=16, pady=12)
        tb.Button(btn_row, text="➕ Thêm clip KOL mới", bootstyle="success",
                  command=self.add_template).pack(side="left")
        tb.Button(btn_row, text="🔁 Đổi video / sửa khung", bootstyle="info-outline",
                  command=self.edit_selected).pack(side="left", padx=8)
        tb.Button(btn_row, text="🗑 Xoá đã chọn", bootstyle="danger-outline",
                  command=self.remove_selected).pack(side="left", padx=8)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")

        self._reload()
        self.grab_set()

    def _reload(self):
        self.listbox.delete(*self.listbox.get_children())
        for i, t in enumerate(self.templates):
            box = t.get("box") or []
            box_text = ",".join(str(v) for v in box) if box else "?"
            self.listbox.insert("", "end", iid=str(i), values=(t.get("name", "?"), t.get("video", "?"), box_text))

    @staticmethod
    def _default_left_box(frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """Vùng mặc định gợi ý đè đúng góc dưới-trái màn hình (tỉ lệ giống KOL_BOX
        chuẩn hiện tại - x=0, sát mép dưới), áp theo kích thước khung hình thực tế
        của video đang thêm/sửa."""
        w = max(1, int(frame_w * 0.537))
        h = max(1, int(frame_h * 0.502))
        x = 0
        y = frame_h - h
        return x, y, w, h

    def _extract_first_frame(self, video: str) -> Path | None:
        frame_path = BASE_DIR / "_tmp_kol_frame_pick.png"
        frame_path.unlink(missing_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-i", video, "-frames:v", "1", str(frame_path)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return frame_path if frame_path.exists() else None

    def _open_template_editor(self, video: str, box: tuple[int, int, int, int],
                               existing: dict | None, existing_index: int | None):
        corner_names = ["Trên-trái", "Trên-phải", "Dưới-phải", "Dưới-trái"]
        existing_corners = (existing or {}).get("corners") or [False, True, False, False]
        corner_vars = [tk.BooleanVar(value=bool(existing_corners[i])) for i in range(4)]
        name_var = tb.StringVar(value=(existing or {}).get("name") or Path(video).stem)
        radius_var = tb.StringVar(value=str((existing or {}).get("radius", 40)))

        detail = tb.Toplevel(self)
        detail.title("Thông tin clip KOL")
        detail.resizable(False, False)
        tb.Label(detail, text="Tên clip (để phân biệt)", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4)
        )
        tb.Entry(detail, textvariable=name_var).pack(fill="x", padx=14)
        tb.Label(detail, text="Góc nào trong khung được bo tròn?", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        corner_row = tb.Frame(detail)
        corner_row.pack(fill="x", padx=14)
        for i, label in enumerate(corner_names):
            tb.Checkbutton(corner_row, text=label, variable=corner_vars[i], bootstyle="round-toggle").pack(
                side="left", padx=(0, 10)
            )
        radius_row = tb.Frame(detail)
        radius_row.pack(fill="x", padx=14, pady=(12, 0))
        tb.Label(radius_row, text="Bán kính bo góc (px):", font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Entry(radius_row, textvariable=radius_var, width=8).pack(side="left", padx=8)

        def save():
            try:
                radius = max(0, int(radius_var.get().strip()))
            except ValueError:
                messagebox.showwarning("Sai định dạng", "Bán kính bo góc phải là số nguyên.")
                return
            name = name_var.get().strip() or Path(video).stem
            entry = {
                "name": name,
                "video": video,
                "box": list(box),
                "corners": [v.get() for v in corner_vars],
                "radius": radius,
            }
            if existing_index is None:
                self.templates.append(entry)
            else:
                self.templates[existing_index] = entry
            core.save_kol_templates(BASE_DIR, self.templates)
            self._reload()
            detail.destroy()

        btn_row2 = tb.Frame(detail)
        btn_row2.pack(fill="x", padx=14, pady=14)
        tb.Button(btn_row2, text="Huỷ", bootstyle="secondary-outline", command=detail.destroy).pack(side="right")
        tb.Button(btn_row2, text="✓ Lưu clip KOL", bootstyle="success", command=save).pack(side="right", padx=8)
        detail.grab_set()

    def add_template(self):
        video = filedialog.askopenfilename(
            title="Chọn video clip KOL (người chỉ tay/phản ứng)",
            filetypes=[("Video", "*.mp4 *.mov"), ("Tất cả", "*.*")],
        )
        if not video:
            return

        frame_path = self._extract_first_frame(video)
        if not frame_path:
            messagebox.showerror("Lỗi", "Không trích được khung hình đầu từ video này.")
            return
        try:
            img = Image.open(frame_path)
            default_box = self._default_left_box(*img.size)
            picker = BoxPickerDialog(self, frame_path, default_box=default_box)
            self.wait_window(picker)
            if not picker.result:
                return
            box = picker.result
        finally:
            frame_path.unlink(missing_ok=True)

        self._open_template_editor(video, box, existing=None, existing_index=None)

    def edit_selected(self):
        sel = self.listbox.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Hãy chọn 1 clip KOL trong danh sách trước.")
            return
        idx = int(sel[0])
        existing = self.templates[idx]

        video = filedialog.askopenfilename(
            title="Chọn video khác cho clip KOL này (hoặc Huỷ để giữ video cũ)",
            filetypes=[("Video", "*.mp4 *.mov"), ("Tất cả", "*.*")],
        )
        if not video:
            video = existing.get("video")
        if not video:
            return

        frame_path = self._extract_first_frame(video)
        if not frame_path:
            messagebox.showerror("Lỗi", "Không trích được khung hình đầu từ video này.")
            return
        try:
            img = Image.open(frame_path)
            old_box = existing.get("box")
            default_box = tuple(old_box) if old_box else self._default_left_box(*img.size)
            picker = BoxPickerDialog(self, frame_path, default_box=default_box)
            self.wait_window(picker)
            if not picker.result:
                return
            box = picker.result
        finally:
            frame_path.unlink(missing_ok=True)

        self._open_template_editor(video, box, existing=existing, existing_index=idx)

    def remove_selected(self):
        sel = self.listbox.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn", "Hãy chọn 1 clip KOL trong danh sách trước.")
            return
        indices = sorted((int(i) for i in sel), reverse=True)
        for idx in indices:
            del self.templates[idx]
        core.save_kol_templates(BASE_DIR, self.templates)
        self._reload()


class HookOneDialog(tb.Toplevel):
    """Tạo hàng loạt video kiểu "Hook 1": ảnh kết quả chạy full màn hình + khung
    "Before" nhỏ góc dưới-trái (ảnh gốc) + mũi tên chỉ vào, KHÔNG có tutorial/khung
    upload."""

    def __init__(self, master):
        super().__init__(master)
        self.title("🎯 Tạo video Kiểu 2 (khung Before)")
        self.geometry("760x620")
        self.minsize(640, 520)
        self.resizable(True, True)
        self.running = False
        self.log_queue = queue.Queue()

        pad = {"padx": 16, "pady": (12, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        self.run_btn = tb.Button(btn_row, text="▶ Bắt đầu tạo video", bootstyle="success",
                                  command=self.start)
        self.run_btn.pack(side="right", padx=8)

        existing = core.load_hook1_config(BASE_DIR)

        tb.Label(
            self,
            text="Nguyên liệu cần có:\n"
                 "  1) Thư mục ẢNH GỐC (before) - mỗi file = 1 video sẽ được tạo.\n"
                 "  2) Thư mục ẢNH KẾT QUẢ (after) - mỗi ảnh kết quả phải TRÙNG TÊN FILE với ảnh gốc.\n"
                 "  3) (Tuỳ chọn) Video logo bay / outtro / nhạc nền / khung upload riêng.\n"
                 "Thứ tự video: ảnh kết quả chạy full màn hình kèm khung Before (ảnh gốc) + mũi tên\n"
                 "chỉ vào  →  đoạn \"tải ảnh lên\" (ảnh gốc trượt vào khung)  →  replay lại các ảnh\n"
                 "kết quả đầu tiên chạy full màn hình bình thường (không còn khung Before).",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        def folder_row(label_text, initial, pick_title):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📁 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(filedialog.askdirectory(title=pick_title) or var.get())
                      ).pack(side="left", padx=(8, 0))
            return var

        def file_row(label_text, initial, pick_title, filetypes):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📄 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(
                          filedialog.askopenfilename(title=pick_title, filetypes=filetypes) or var.get()
                      )).pack(side="left", padx=(8, 0))
            tb.Button(row, text="✕ Bỏ", bootstyle="secondary-outline",
                      command=lambda: var.set("")).pack(side="left", padx=(8, 0))
            return var

        self.originals_var = folder_row(
            "1) Thư mục ảnh gốc (before)", existing.get("originals_dir"),
            "Chọn thư mục ảnh gốc",
        )
        self.results_var = folder_row(
            "2) Thư mục ảnh kết quả (after, tên file trùng với ảnh gốc)", existing.get("results_dir"),
            "Chọn thư mục ảnh kết quả",
        )
        self.out_var = folder_row(
            "3) Thư mục lưu video đầu ra", existing.get("video_out_dir"),
            "Chọn thư mục lưu video",
        )
        default_logo = str(DEFAULT_LOGO_PATH) if DEFAULT_LOGO_PATH.exists() else ""
        default_outtro = str(DEFAULT_OUTTRO_PATH) if DEFAULT_OUTTRO_PATH.exists() else ""

        self.logo_var = file_row(
            "4) Video logo bay (.mov, có alpha) - tuỳ chọn (mặc định có sẵn)",
            existing.get("logo_path") or default_logo,
            "Chọn video logo bay", [("Video", "*.mov *.mp4"), ("Tất cả", "*.*")],
        )
        self.outtro_var = file_row(
            "5) Video outtro - tuỳ chọn (mặc định có sẵn)",
            existing.get("outtro_path") or default_outtro,
            "Chọn video outtro", [("Video", "*.mp4 *.mov"), ("Tất cả", "*.*")],
        )
        self.audio_var = file_row(
            "6) Nhạc nền - tuỳ chọn",
            existing.get("audio_path"),
            "Chọn file nhạc nền", [("Audio", "*.mp3 *.m4a *.wav *.aac"), ("Tất cả", "*.*")],
        )
        self.upload_frame_var = file_row(
            "7) Ảnh khung upload tuỳ chỉnh - tuỳ chọn (mặc định dùng khung có sẵn của tool)",
            existing.get("upload_frame_path"),
            "Chọn ảnh khung upload", [("Ảnh", "*.png *.jpg *.jpeg"), ("Tất cả", "*.*")],
        )

        count_row = tb.Frame(self)
        count_row.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(count_row, text="Số ảnh kết quả / video:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.result_count_var = tb.StringVar(value=str(existing.get("result_count", 2)))
        result_count_entry = tb.Entry(count_row, textvariable=self.result_count_var, width=6)
        result_count_entry.pack(side="left", padx=8)
        result_count_entry.bind("<FocusOut>", lambda e: self._rebuild_transition_pickers())
        result_count_entry.bind("<Return>", lambda e: self._rebuild_transition_pickers())

        tb.Label(count_row, text="Số ảnh replay sau khi tải lên:",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.replay_count_var = tb.StringVar(value=str(existing.get("replay_count", 2)))
        replay_count_entry = tb.Entry(count_row, textvariable=self.replay_count_var, width=6)
        replay_count_entry.pack(side="left", padx=8)
        replay_count_entry.bind("<FocusOut>", lambda e: self._rebuild_replay_transition_pickers())
        replay_count_entry.bind("<Return>", lambda e: self._rebuild_replay_transition_pickers())

        count_row2 = tb.Frame(self)
        count_row2.pack(fill="x", padx=16, pady=(8, 0))
        tb.Label(count_row2, text="Số video chạy song song:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.workers_var = tb.StringVar(value=str(existing.get("workers", 2)))
        tb.Entry(count_row2, textvariable=self.workers_var, width=6).pack(side="left", padx=8)

        tb.Label(count_row2, text="Âm lượng nhạc nền (0-1):",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.audio_volume_var = tb.StringVar(value=str(existing.get("audio_volume", 0.5)))
        tb.Entry(count_row2, textvariable=self.audio_volume_var, width=6).pack(side="left", padx=8)

        transition_header = tb.Frame(self)
        transition_header.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(transition_header, text="Hiệu ứng chuyển cảnh giữa TỪNG CẶP ảnh kết quả:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Button(transition_header, text="🔄 Cập nhật theo số ảnh", bootstyle="secondary-outline",
                  command=self._rebuild_transition_pickers).pack(side="left", padx=8)

        self._transition_label_for_value = {v: k for k, v in TRANSITION_OPTIONS.items()}
        saved_transitions = existing.get("transitions")
        if not saved_transitions and existing.get("transition"):
            saved_transitions = [existing["transition"]]
        self._saved_transition_labels = [
            self._transition_label_for_value.get(t, next(iter(TRANSITION_OPTIONS)))
            for t in (saved_transitions or [])
        ]
        self.transition_picker_frame = tb.Frame(self)
        self.transition_picker_frame.pack(fill="x", padx=16, pady=(4, 0))
        self.transition_vars: list[tb.StringVar] = []
        self._rebuild_transition_pickers()

        replay_transition_header = tb.Frame(self)
        replay_transition_header.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(replay_transition_header, text="Hiệu ứng chuyển cảnh giữa TỪNG CẶP ảnh replay (sau khi tải lên):",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Button(replay_transition_header, text="🔄 Cập nhật theo số ảnh", bootstyle="secondary-outline",
                  command=self._rebuild_replay_transition_pickers).pack(side="left", padx=8)

        saved_replay_transitions = existing.get("replay_transitions")
        self._saved_replay_transition_labels = [
            self._transition_label_for_value.get(t, next(iter(TRANSITION_OPTIONS)))
            for t in (saved_replay_transitions or [])
        ]
        self.replay_transition_picker_frame = tb.Frame(self)
        self.replay_transition_picker_frame.pack(fill="x", padx=16, pady=(4, 0))
        self.replay_transition_vars: list[tb.StringVar] = []
        self._rebuild_replay_transition_pickers()

        self.status_var = tb.StringVar(value="Sẵn sàng.")
        tb.Label(self, textvariable=self.status_var, bootstyle="secondary").pack(
            anchor="w", padx=16, pady=(10, 0)
        )

        log_frame = tb.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self.log_text = tb.Text(log_frame, state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                                 wrap="word", font=("Consolas", 9))
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("warn", foreground="#fbbf24")
        sb = tb.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.bind("<Escape>", lambda e: self.destroy())
        self.after(100, self._poll_log_queue)
        self.grab_set()

    def _append_log(self, text: str) -> None:
        tag = None
        low = text.lower()
        if "lỗi" in low:
            tag = "err"
        elif "cảnh báo" in low or "bỏ qua" in low:
            tag = "warn"
        elif "xong" in low:
            tag = "ok"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _rebuild_transition_pickers(self):
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            result_count = 2
        n_pairs = max(1, result_count - 1)

        old_labels = [v.get() for v in self.transition_vars]
        for w in self.transition_picker_frame.winfo_children():
            w.destroy()
        self.transition_vars = []

        for i in range(n_pairs):
            if i < len(old_labels):
                default_label = old_labels[i]
            elif i < len(self._saved_transition_labels):
                default_label = self._saved_transition_labels[i]
            else:
                default_label = next(iter(TRANSITION_OPTIONS))
            row = tb.Frame(self.transition_picker_frame)
            row.pack(fill="x", pady=2)
            tb.Label(row, text=f"Ảnh {i + 1} → Ảnh {i + 2}:", width=16, anchor="w").pack(side="left")
            var = tb.StringVar(value=default_label)
            tb.Combobox(row, textvariable=var, state="readonly",
                        values=list(TRANSITION_OPTIONS.keys()), width=28).pack(side="left", padx=8)
            self.transition_vars.append(var)

    def _rebuild_replay_transition_pickers(self):
        try:
            replay_count = max(0, int(self.replay_count_var.get().strip()))
        except ValueError:
            replay_count = 2
        n_pairs = max(1, replay_count - 1)

        old_labels = [v.get() for v in self.replay_transition_vars]
        for w in self.replay_transition_picker_frame.winfo_children():
            w.destroy()
        self.replay_transition_vars = []

        for i in range(n_pairs):
            if i < len(old_labels):
                default_label = old_labels[i]
            elif i < len(self._saved_replay_transition_labels):
                default_label = self._saved_replay_transition_labels[i]
            else:
                default_label = next(iter(TRANSITION_OPTIONS))
            row = tb.Frame(self.replay_transition_picker_frame)
            row.pack(fill="x", pady=2)
            tb.Label(row, text=f"Replay {i + 1} → Replay {i + 2}:", width=16, anchor="w").pack(side="left")
            var = tb.StringVar(value=default_label)
            tb.Combobox(row, textvariable=var, state="readonly",
                        values=list(TRANSITION_OPTIONS.keys()), width=28).pack(side="left", padx=8)
            self.replay_transition_vars.append(var)

    def start(self):
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tiến trình tạo video chạy, vui lòng đợi xong.")
            return
        originals_dir = self.originals_var.get().strip()
        results_dir = self.results_var.get().strip()
        out_dir = self.out_var.get().strip()
        logo_path = self.logo_var.get().strip() or None
        outtro_path = self.outtro_var.get().strip() or None
        audio_path = self.audio_var.get().strip() or None
        upload_frame_path = self.upload_frame_var.get().strip() or None

        if not originals_dir or not results_dir or not out_dir:
            messagebox.showwarning(
                "Thiếu thông tin",
                "Hãy chọn đủ: thư mục ảnh gốc, thư mục ảnh kết quả, và thư mục lưu video.",
            )
            return
        try:
            workers = max(1, int(self.workers_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số video chạy song song phải là số nguyên, ví dụ 1, 2, 3.")
            return
        try:
            audio_volume = min(1.0, max(0.0, float(self.audio_volume_var.get().strip())))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Âm lượng nhạc nền phải là số từ 0 đến 1, ví dụ 0.5.")
            return
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số ảnh kết quả / video phải là số nguyên, ví dụ 2, 3, 4.")
            return
        try:
            replay_count = max(0, int(self.replay_count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số ảnh replay sau khi tải lên phải là số nguyên, ví dụ 2.")
            return

        self._rebuild_transition_pickers()
        transitions = [TRANSITION_OPTIONS.get(v.get(), "slideright") for v in self.transition_vars]
        self._rebuild_replay_transition_pickers()
        replay_transitions = [TRANSITION_OPTIONS.get(v.get(), "slideright") for v in self.replay_transition_vars]

        core.save_hook1_config(
            BASE_DIR, originals_dir=originals_dir, results_dir=results_dir,
            video_out_dir=out_dir, logo_path=logo_path, outtro_path=outtro_path,
            audio_path=audio_path, audio_volume=audio_volume, workers=workers,
            result_count=result_count, replay_count=replay_count,
            upload_frame_path=upload_frame_path, transitions=transitions,
            replay_transitions=replay_transitions,
        )

        self.running = True
        self.run_btn.configure(state="disabled")
        self.status_var.set("Đang chạy...")
        threading.Thread(
            target=self._worker,
            args=(originals_dir, results_dir, out_dir, logo_path, outtro_path,
                  audio_path, audio_volume, result_count, replay_count,
                  upload_frame_path, transitions, replay_transitions, workers),
            daemon=True,
        ).start()

    def _worker(self, originals_dir, results_dir, out_dir, logo_path, outtro_path,
                audio_path, audio_volume, result_count, replay_count,
                upload_frame_path, transitions, replay_transitions, workers):
        def log(msg: str) -> None:
            self.log_queue.put(msg)

        try:
            outputs = video_core.auto_before_badge_from_folders(
                Path(originals_dir), Path(results_dir), Path(out_dir),
                logo_path=Path(logo_path) if logo_path else None,
                outtro_path=Path(outtro_path) if outtro_path else None,
                audio_path=Path(audio_path) if audio_path else None,
                audio_volume=audio_volume,
                result_count=result_count,
                replay_count=replay_count,
                upload_frame_path=Path(upload_frame_path) if upload_frame_path else None,
                transition=transitions,
                replay_transition=replay_transitions,
                on_log=log,
                max_workers=workers,
            )
            log(f"===== XONG: đã tạo {len(outputs)} video =====")
            self.after(0, lambda: self.status_var.set(f"Xong: đã tạo {len(outputs)} video."))
        except Exception as exc:
            log(f"LỖI: {exc}")
            self.after(0, lambda: self.status_var.set("Lỗi, xem log bên dưới."))
        finally:
            self.running = False
            self.after(0, lambda: self.run_btn.configure(state="normal"))


class HookThreeDialog(tb.Toplevel):
    """Tạo hàng loạt video kiểu "Hook 3": giống hệt Hook 1 (2-3 ảnh kèm khung Before →
    tải ảnh lên → replay), nhưng CHÈN THÊM 1 đoạn "KOL chỉ tay vào ảnh kết quả" ngay sau
    nhóm ảnh Before và trước khi tải ảnh lên. Mỗi video chọn NGẪU NHIÊN 1 clip KOL trong
    danh sách đã khai báo ở "🗣 Quản lý clip KOL"."""

    def __init__(self, master):
        super().__init__(master)
        self.title("🗣 Tạo video Kiểu 3 (Before + KOL)")
        self.geometry("760x620")
        self.minsize(640, 520)
        self.resizable(True, True)
        self.running = False
        self.log_queue = queue.Queue()

        pad = {"padx": 16, "pady": (12, 4)}

        btn_row = tb.Frame(self)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=16)
        tb.Button(btn_row, text="Đóng", bootstyle="secondary-outline",
                  command=self.destroy).pack(side="right")
        self.run_btn = tb.Button(btn_row, text="▶ Bắt đầu tạo video", bootstyle="success",
                                  command=self.start)
        self.run_btn.pack(side="right", padx=8)

        existing = core.load_hook3_config(BASE_DIR)

        tb.Label(
            self,
            text="Nguyên liệu cần có:\n"
                 "  1) Thư mục ẢNH GỐC (before) - mỗi file = 1 video sẽ được tạo.\n"
                 "  2) Thư mục ẢNH KẾT QUẢ (after) - mỗi ảnh kết quả phải TRÙNG TÊN FILE với ảnh gốc.\n"
                 "  3) Ít nhất 1 clip KOL đã khai báo ở \"🗣 Quản lý clip KOL\".\n"
                 "  4) (Tuỳ chọn) Video logo bay / outtro / nhạc nền / khung upload riêng.\n"
                 "Thứ tự video: ảnh kết quả kèm khung Before + mũi tên  →  đoạn KOL chỉ tay vào\n"
                 "ảnh kết quả  →  \"tải ảnh lên\"  →  replay lại ảnh kết quả (không còn khung Before).",
            bootstyle="secondary", justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        def folder_row(label_text, initial, pick_title):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📁 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(filedialog.askdirectory(title=pick_title) or var.get())
                      ).pack(side="left", padx=(8, 0))
            return var

        def file_row(label_text, initial, pick_title, filetypes):
            tb.Label(self, text=label_text, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
            row = tb.Frame(self)
            row.pack(fill="x", padx=16)
            var = tb.StringVar(value=initial or "")
            tb.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
            tb.Button(row, text="📄 Chọn...", bootstyle="secondary",
                      command=lambda: var.set(
                          filedialog.askopenfilename(title=pick_title, filetypes=filetypes) or var.get()
                      )).pack(side="left", padx=(8, 0))
            tb.Button(row, text="✕ Bỏ", bootstyle="secondary-outline",
                      command=lambda: var.set("")).pack(side="left", padx=(8, 0))
            return var

        self.originals_var = folder_row(
            "1) Thư mục ảnh gốc (before)", existing.get("originals_dir"),
            "Chọn thư mục ảnh gốc",
        )
        self.results_var = folder_row(
            "2) Thư mục ảnh kết quả (after, tên file trùng với ảnh gốc)", existing.get("results_dir"),
            "Chọn thư mục ảnh kết quả",
        )
        self.out_var = folder_row(
            "3) Thư mục lưu video đầu ra", existing.get("video_out_dir"),
            "Chọn thư mục lưu video",
        )
        default_logo = str(DEFAULT_LOGO_PATH) if DEFAULT_LOGO_PATH.exists() else ""
        default_outtro = str(DEFAULT_OUTTRO_PATH) if DEFAULT_OUTTRO_PATH.exists() else ""

        self.logo_var = file_row(
            "4) Video logo bay (.mov, có alpha) - tuỳ chọn (mặc định có sẵn)",
            existing.get("logo_path") or default_logo,
            "Chọn video logo bay", [("Video", "*.mov *.mp4"), ("Tất cả", "*.*")],
        )
        self.outtro_var = file_row(
            "5) Video outtro - tuỳ chọn (mặc định có sẵn)",
            existing.get("outtro_path") or default_outtro,
            "Chọn video outtro", [("Video", "*.mp4 *.mov"), ("Tất cả", "*.*")],
        )
        self.audio_var = file_row(
            "6) Nhạc nền - tuỳ chọn",
            existing.get("audio_path"),
            "Chọn file nhạc nền", [("Audio", "*.mp3 *.m4a *.wav *.aac"), ("Tất cả", "*.*")],
        )
        self.upload_frame_var = file_row(
            "7) Ảnh khung upload tuỳ chỉnh - tuỳ chọn (mặc định dùng khung có sẵn của tool)",
            existing.get("upload_frame_path"),
            "Chọn ảnh khung upload", [("Ảnh", "*.png *.jpg *.jpeg"), ("Tất cả", "*.*")],
        )

        kol_hint_row = tb.Frame(self)
        kol_hint_row.pack(fill="x", padx=16, pady=(10, 0))
        n_kol = len(core.load_kol_templates(BASE_DIR))
        self.kol_hint_var = tb.StringVar(
            value=f"Đang có {n_kol} clip KOL khả dụng." if n_kol
            else "⚠ Chưa có clip KOL nào - hãy thêm ở \"🗣 Quản lý clip KOL\" trước khi chạy."
        )
        tb.Label(kol_hint_row, textvariable=self.kol_hint_var,
                 bootstyle=("success" if n_kol else "danger")).pack(side="left")

        count_row = tb.Frame(self)
        count_row.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(count_row, text="Số ảnh kết quả / video:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.result_count_var = tb.StringVar(value=str(existing.get("result_count", 2)))
        result_count_entry = tb.Entry(count_row, textvariable=self.result_count_var, width=6)
        result_count_entry.pack(side="left", padx=8)
        result_count_entry.bind("<FocusOut>", lambda e: self._rebuild_transition_pickers())
        result_count_entry.bind("<Return>", lambda e: self._rebuild_transition_pickers())

        tb.Label(count_row, text="Số ảnh replay sau khi tải lên:",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.replay_count_var = tb.StringVar(value=str(existing.get("replay_count", 2)))
        replay_count_entry = tb.Entry(count_row, textvariable=self.replay_count_var, width=6)
        replay_count_entry.pack(side="left", padx=8)
        replay_count_entry.bind("<FocusOut>", lambda e: self._rebuild_replay_transition_pickers())
        replay_count_entry.bind("<Return>", lambda e: self._rebuild_replay_transition_pickers())

        count_row2 = tb.Frame(self)
        count_row2.pack(fill="x", padx=16, pady=(8, 0))
        tb.Label(count_row2, text="Số video chạy song song:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.workers_var = tb.StringVar(value=str(existing.get("workers", 2)))
        tb.Entry(count_row2, textvariable=self.workers_var, width=6).pack(side="left", padx=8)

        tb.Label(count_row2, text="Âm lượng nhạc nền (0-1):",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(24, 0))
        self.audio_volume_var = tb.StringVar(value=str(existing.get("audio_volume", 0.5)))
        tb.Entry(count_row2, textvariable=self.audio_volume_var, width=6).pack(side="left", padx=8)

        transition_header = tb.Frame(self)
        transition_header.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(transition_header, text="Hiệu ứng chuyển cảnh giữa TỪNG CẶP ảnh kết quả:",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Button(transition_header, text="🔄 Cập nhật theo số ảnh", bootstyle="secondary-outline",
                  command=self._rebuild_transition_pickers).pack(side="left", padx=8)

        self._transition_label_for_value = {v: k for k, v in TRANSITION_OPTIONS.items()}
        saved_transitions = existing.get("transitions")
        if not saved_transitions and existing.get("transition"):
            saved_transitions = [existing["transition"]]
        self._saved_transition_labels = [
            self._transition_label_for_value.get(t, next(iter(TRANSITION_OPTIONS)))
            for t in (saved_transitions or [])
        ]
        self.transition_picker_frame = tb.Frame(self)
        self.transition_picker_frame.pack(fill="x", padx=16, pady=(4, 0))
        self.transition_vars: list[tb.StringVar] = []
        self._rebuild_transition_pickers()

        replay_transition_header = tb.Frame(self)
        replay_transition_header.pack(fill="x", padx=16, pady=(10, 0))
        tb.Label(replay_transition_header, text="Hiệu ứng chuyển cảnh giữa TỪNG CẶP ảnh replay (sau khi tải lên):",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tb.Button(replay_transition_header, text="🔄 Cập nhật theo số ảnh", bootstyle="secondary-outline",
                  command=self._rebuild_replay_transition_pickers).pack(side="left", padx=8)

        saved_replay_transitions = existing.get("replay_transitions")
        self._saved_replay_transition_labels = [
            self._transition_label_for_value.get(t, next(iter(TRANSITION_OPTIONS)))
            for t in (saved_replay_transitions or [])
        ]
        self.replay_transition_picker_frame = tb.Frame(self)
        self.replay_transition_picker_frame.pack(fill="x", padx=16, pady=(4, 0))
        self.replay_transition_vars: list[tb.StringVar] = []
        self._rebuild_replay_transition_pickers()

        self.status_var = tb.StringVar(value="Sẵn sàng.")
        tb.Label(self, textvariable=self.status_var, bootstyle="secondary").pack(
            anchor="w", padx=16, pady=(10, 0)
        )

        log_frame = tb.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self.log_text = tb.Text(log_frame, state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                                 wrap="word", font=("Consolas", 9))
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("warn", foreground="#fbbf24")
        sb = tb.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.bind("<Escape>", lambda e: self.destroy())
        self.after(100, self._poll_log_queue)
        self.grab_set()

    def _append_log(self, text: str) -> None:
        tag = None
        low = text.lower()
        if "lỗi" in low:
            tag = "err"
        elif "cảnh báo" in low or "bỏ qua" in low:
            tag = "warn"
        elif "xong" in low:
            tag = "ok"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _rebuild_transition_pickers(self):
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            result_count = 2
        n_pairs = max(1, result_count - 1)

        old_labels = [v.get() for v in self.transition_vars]
        for w in self.transition_picker_frame.winfo_children():
            w.destroy()
        self.transition_vars = []

        for i in range(n_pairs):
            if i < len(old_labels):
                default_label = old_labels[i]
            elif i < len(self._saved_transition_labels):
                default_label = self._saved_transition_labels[i]
            else:
                default_label = next(iter(TRANSITION_OPTIONS))
            row = tb.Frame(self.transition_picker_frame)
            row.pack(fill="x", pady=2)
            tb.Label(row, text=f"Ảnh {i + 1} → Ảnh {i + 2}:", width=16, anchor="w").pack(side="left")
            var = tb.StringVar(value=default_label)
            tb.Combobox(row, textvariable=var, state="readonly",
                        values=list(TRANSITION_OPTIONS.keys()), width=28).pack(side="left", padx=8)
            self.transition_vars.append(var)

    def _rebuild_replay_transition_pickers(self):
        try:
            replay_count = max(0, int(self.replay_count_var.get().strip()))
        except ValueError:
            replay_count = 2
        n_pairs = max(1, replay_count - 1)

        old_labels = [v.get() for v in self.replay_transition_vars]
        for w in self.replay_transition_picker_frame.winfo_children():
            w.destroy()
        self.replay_transition_vars = []

        for i in range(n_pairs):
            if i < len(old_labels):
                default_label = old_labels[i]
            elif i < len(self._saved_replay_transition_labels):
                default_label = self._saved_replay_transition_labels[i]
            else:
                default_label = next(iter(TRANSITION_OPTIONS))
            row = tb.Frame(self.replay_transition_picker_frame)
            row.pack(fill="x", pady=2)
            tb.Label(row, text=f"Replay {i + 1} → Replay {i + 2}:", width=16, anchor="w").pack(side="left")
            var = tb.StringVar(value=default_label)
            tb.Combobox(row, textvariable=var, state="readonly",
                        values=list(TRANSITION_OPTIONS.keys()), width=28).pack(side="left", padx=8)
            self.replay_transition_vars.append(var)

    def start(self):
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tiến trình tạo video chạy, vui lòng đợi xong.")
            return
        originals_dir = self.originals_var.get().strip()
        results_dir = self.results_var.get().strip()
        out_dir = self.out_var.get().strip()
        logo_path = self.logo_var.get().strip() or None
        outtro_path = self.outtro_var.get().strip() or None
        audio_path = self.audio_var.get().strip() or None
        upload_frame_path = self.upload_frame_var.get().strip() or None

        if not originals_dir or not results_dir or not out_dir:
            messagebox.showwarning(
                "Thiếu thông tin",
                "Hãy chọn đủ: thư mục ảnh gốc, thư mục ảnh kết quả, và thư mục lưu video.",
            )
            return
        kol_templates = core.load_kol_templates(BASE_DIR)
        if not kol_templates:
            messagebox.showwarning(
                "Chưa có clip KOL",
                "Hãy bấm \"🗣 Quản lý clip KOL\" để thêm ít nhất 1 clip KOL trước khi chạy Hook 3.",
            )
            return
        try:
            workers = max(1, int(self.workers_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số video chạy song song phải là số nguyên, ví dụ 1, 2, 3.")
            return
        try:
            audio_volume = min(1.0, max(0.0, float(self.audio_volume_var.get().strip())))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Âm lượng nhạc nền phải là số từ 0 đến 1, ví dụ 0.5.")
            return
        try:
            result_count = max(1, int(self.result_count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số ảnh kết quả / video phải là số nguyên, ví dụ 2, 3, 4.")
            return
        try:
            replay_count = max(0, int(self.replay_count_var.get().strip()))
        except ValueError:
            messagebox.showwarning("Sai định dạng", "Số ảnh replay sau khi tải lên phải là số nguyên, ví dụ 2.")
            return

        self._rebuild_transition_pickers()
        transitions = [TRANSITION_OPTIONS.get(v.get(), "slideright") for v in self.transition_vars]
        self._rebuild_replay_transition_pickers()
        replay_transitions = [TRANSITION_OPTIONS.get(v.get(), "slideright") for v in self.replay_transition_vars]

        core.save_hook3_config(
            BASE_DIR, originals_dir=originals_dir, results_dir=results_dir,
            video_out_dir=out_dir, logo_path=logo_path, outtro_path=outtro_path,
            audio_path=audio_path, audio_volume=audio_volume, workers=workers,
            result_count=result_count, replay_count=replay_count,
            upload_frame_path=upload_frame_path, transitions=transitions,
            replay_transitions=replay_transitions,
        )

        self.running = True
        self.run_btn.configure(state="disabled")
        self.status_var.set("Đang chạy...")
        threading.Thread(
            target=self._worker,
            args=(originals_dir, results_dir, out_dir, logo_path, outtro_path,
                  audio_path, audio_volume, result_count, replay_count,
                  upload_frame_path, transitions, kol_templates, replay_transitions, workers),
            daemon=True,
        ).start()

    def _worker(self, originals_dir, results_dir, out_dir, logo_path, outtro_path,
                audio_path, audio_volume, result_count, replay_count,
                upload_frame_path, transitions, kol_templates, replay_transitions, workers):
        def log(msg: str) -> None:
            self.log_queue.put(msg)

        try:
            outputs = video_core.auto_before_badge_from_folders(
                Path(originals_dir), Path(results_dir), Path(out_dir),
                logo_path=Path(logo_path) if logo_path else None,
                outtro_path=Path(outtro_path) if outtro_path else None,
                audio_path=Path(audio_path) if audio_path else None,
                audio_volume=audio_volume,
                result_count=result_count,
                replay_count=replay_count,
                kol_templates=kol_templates,
                upload_frame_path=Path(upload_frame_path) if upload_frame_path else None,
                transition=transitions,
                replay_transition=replay_transitions,
                on_log=log,
                max_workers=workers,
            )
            log(f"===== XONG: đã tạo {len(outputs)} video =====")
            self.after(0, lambda: self.status_var.set(f"Xong: đã tạo {len(outputs)} video."))
        except Exception as exc:
            log(f"LỖI: {exc}")
            self.after(0, lambda: self.status_var.set("Lỗi, xem log bên dưới."))
        finally:
            self.running = False
            self.after(0, lambda: self.run_btn.configure(state="normal"))


class App(tb.Window):
    def __init__(self):
        super().__init__(themename="flatly")
        self.title("Gen Image Tool")
        self.geometry("960x680")
        self.minsize(820, 560)

        self.jobs = []
        self.log_queue = queue.Queue()
        self.running = False
        self.stop_requested = False

        self._build_ui()
        self.after(100, self._poll_log_queue)
        self.bind("<FocusIn>", self._clear_taskbar_badge)

        self.accounts = core.load_accounts_file(BASE_DIR)
        self._update_accounts_label()
        self.muse_config = core.load_muse_config(BASE_DIR)
        self.output_dir = core.load_output_dir(BASE_DIR) or OUTPUT_DIR
        self._update_output_dir_label()

        self.codex_bin = core.find_codex_bin()
        if not self.codex_bin:
            self.after(300, lambda: messagebox.showwarning(
                "Thiếu Codex CLI",
                "Không tìm thấy lệnh 'codex'. Hãy cài Codex CLI và đăng nhập (codex login) "
                "trước khi chạy gen ảnh. Xem HUONG_DAN_SU_DUNG.txt.",
            ))

    def _build_ui(self):
        header = tb.Frame(self, bootstyle="primary")
        header.pack(fill="x")
        tb.Label(header, text="🎨  Gen Image Tool", font=("Segoe UI", 18, "bold"),
                 bootstyle="inverse-primary").pack(side="left", padx=20, pady=14)
        self.codex_status = tb.Label(header, text="", font=("Segoe UI", 9),
                                      bootstyle="inverse-primary")
        self.codex_status.pack(side="right", padx=20)
        self.accounts_status = tb.Label(header, text="", font=("Segoe UI", 9),
                                         bootstyle="inverse-primary")
        self.accounts_status.pack(side="right", padx=(0, 4))
        self.output_dir_status = tb.Label(header, text="", font=("Segoe UI", 9),
                                           bootstyle="inverse-primary")
        self.output_dir_status.pack(side="right", padx=(0, 4))

        body = tb.Frame(self)
        body.pack(fill="both", expand=True, padx=16, pady=14)

        # --- Khu vực danh sách job ---
        jobs_card = tb.Labelframe(body, text="  Danh sách job  ", bootstyle="primary")
        jobs_card.pack(fill="x")

        toolbar = tb.Frame(jobs_card)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        add_row = tb.Frame(toolbar)
        add_row.pack(fill="x")

        tb.Button(add_row, text="🖼 Ảnh biến thể", bootstyle="success",
                  command=self.add_job).pack(side="left")
        tb.Button(add_row, text="📝 Chỉ prompt", bootstyle="success-outline",
                  command=self.add_text_job).pack(side="left", padx=6)
        tb.Button(add_row, text="📦 Hàng loạt", bootstyle="success-outline",
                  command=self.add_jobs_bulk).pack(side="left", padx=6)
        tb.Button(add_row, text="📊 Excel", bootstyle="success-outline",
                  command=self.import_excel_jobs).pack(side="left", padx=6)

        self.run_btn = tb.Button(add_row, text="▶  Chạy tất cả", bootstyle="success",
                                  command=self.run_all, width=16)
        self.run_btn.pack(side="right")

        self.stop_btn = tb.Button(add_row, text="⏹ Dừng", bootstyle="danger",
                                   command=self.stop_run, width=10, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 8))

        self.run_selected_btn = tb.Button(add_row, text="▶ Job đã chọn", bootstyle="success-outline",
                                           command=self.run_selected, width=16)
        self.run_selected_btn.pack(side="right", padx=(0, 8))

        tb.Separator(toolbar, bootstyle="secondary").pack(fill="x", pady=(10, 8))

        def tool_row(title: str):
            row = tb.Frame(toolbar)
            row.pack(fill="x", pady=3)
            tb.Label(row, text=title, width=15, anchor="w",
                     font=("Segoe UI", 9, "bold"), bootstyle="secondary").pack(side="left")
            return row

        job_row = tool_row("📋 Job đã chọn")
        tb.Button(job_row, text="✏ Sửa", bootstyle="warning-outline",
                  command=self.edit_job).pack(side="left")
        tb.Button(job_row, text="🗑 Xoá", bootstyle="danger-outline",
                  command=self.remove_job).pack(side="left", padx=6)
        tb.Button(job_row, text="📂 Mở thư mục kết quả", bootstyle="info-outline",
                  command=self.open_output_folder).pack(side="left", padx=6)
        tb.Button(job_row, text="📁 Chọn thư mục lưu", bootstyle="info-outline",
                  command=self.choose_output_dir).pack(side="left", padx=6)

        account_row = tool_row("👤 Tài khoản & API")
        tb.Button(account_row, text="👤 Tài khoản Codex", bootstyle="secondary-outline",
                  command=self.manage_accounts).pack(side="left")
        tb.Button(account_row, text="🅜 Cấu hình Muse", bootstyle="secondary-outline",
                  command=self.manage_muse_config).pack(side="left", padx=6)
        tb.Button(account_row, text="🅜 Tài khoản Muse", bootstyle="secondary-outline",
                  command=self.manage_muse_accounts).pack(side="left", padx=6)

        video_row = tool_row("🎬 Tạo video")
        tb.Button(video_row, text="🎬 Kiểu 1", bootstyle="warning-outline",
                  command=self.open_video_maker).pack(side="left")
        tb.Button(video_row, text="🎯 Kiểu 2", bootstyle="warning-outline",
                  command=self.open_hook1_maker).pack(side="left", padx=6)
        tb.Button(video_row, text="🗣 Kiểu 3", bootstyle="warning-outline",
                  command=self.open_hook3_maker).pack(side="left", padx=6)
        tb.Button(video_row, text="🗣 Clip KOL", bootstyle="secondary-outline",
                  command=self.manage_kol_templates).pack(side="left", padx=6)

        columns = ("status", "mode", "name", "folder", "prompt")
        self.tree = tb.Treeview(jobs_card, columns=columns, show="headings", height=6,
                                 bootstyle="primary", selectmode="extended")
        self.tree.heading("status", text="")
        self.tree.heading("mode", text="Loại")
        self.tree.heading("name", text="Tên job")
        self.tree.heading("folder", text="Thư mục ảnh")
        self.tree.heading("prompt", text="Prompt (rút gọn)")
        self.tree.column("status", width=70, anchor="center")
        self.tree.column("mode", width=90, anchor="center")
        self.tree.column("name", width=110)
        self.tree.column("folder", width=260)
        self.tree.column("prompt", width=360)
        self.tree.pack(fill="x", padx=10, pady=(0, 10))
        self.tree.bind("<Double-1>", lambda e: self.edit_job())

        self.empty_hint = tb.Label(
            jobs_card,
            text="Chưa có job nào. Bấm “➕ Thêm job” để bắt đầu.",
            bootstyle="secondary",
        )

        # --- Thanh tiến trình ---
        progress_row = tb.Frame(body)
        progress_row.pack(fill="x", pady=(12, 4))
        self.progress = tb.Progressbar(progress_row, mode="determinate", bootstyle="success-striped")
        self.progress.pack(fill="x", side="left", expand=True)
        self.status_var = tb.StringVar(value="Sẵn sàng.")
        tb.Label(progress_row, textvariable=self.status_var, width=28, anchor="e").pack(side="left", padx=(10, 0))

        # --- Log ---
        log_card = tb.Labelframe(body, text="  Tiến trình  ", bootstyle="primary")
        log_card.pack(fill="both", expand=True, pady=(8, 0))
        log_frame = tb.Frame(log_card)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tb.Text(log_frame, state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                                 insertbackground="#fff", wrap="word", font=("Consolas", 9))
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("warn", foreground="#fbbf24")
        self.log_text.tag_configure("dim", foreground="#9ca3af")
        scroll = tb.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._refresh_jobs_view()

    # ---------- Job list ----------
    def _refresh_jobs_view(self):
        if self.jobs:
            self.empty_hint.pack_forget()
        else:
            self.empty_hint.pack(padx=10, pady=(0, 10))

    def _job_row_values(self, job: dict, status: str = "⏳") -> tuple:
        backend_tag = " 🅜" if job.get("backend") == "muse" else ""
        out_base = str(job["output_dir"]) if job.get("output_dir") else None
        if job.get("mode") == "text":
            mode_label = "📝 Prompt" + backend_tag
            if job.get("sample_image"):
                folder_col = f"(+ ảnh mẫu: {Path(job['sample_image']).name}, x{job.get('count', 1)})"
            else:
                folder_col = f"(không cần ảnh, x{job.get('count', 1)})"
        else:
            mode_label = "🖼 Ảnh mẫu" + backend_tag
            folder_col = str(job["folder"])
        if job.get("output_group"):
            folder_col += f"  →  {out_base or 'output'}\\{job['output_group']}\\"
        elif out_base:
            folder_col += f"  →  {out_base}\\{job['name']}\\"
        preview = job["prompt"].replace("\n", " ")[:60]
        return (status, mode_label, job["name"], folder_col, preview)

    def add_job(self):
        dialog = AddJobDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.jobs.append(dialog.result)
            self.tree.insert("", "end", values=self._job_row_values(dialog.result))
            self._refresh_jobs_view()

    def add_text_job(self):
        dialog = AddTextJobDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.jobs.append(dialog.result)
            self.tree.insert("", "end", values=self._job_row_values(dialog.result))
            self._refresh_jobs_view()

    def add_jobs_bulk(self):
        dialog = BulkAddJobDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            for job in dialog.result:
                self.jobs.append(job)
                self.tree.insert("", "end", values=self._job_row_values(job))
            self._refresh_jobs_view()

    def import_excel_jobs(self):
        dialog = ImportExcelJobsDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            for job in dialog.result:
                self.jobs.append(job)
                self.tree.insert("", "end", values=self._job_row_values(job))
            self._refresh_jobs_view()

    def remove_job(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn job", "Hãy chọn 1 hoặc nhiều job trong danh sách trước "
                                                  "(giữ Ctrl hoặc Shift để chọn nhiều).")
            return
        if len(sel) > 1 and not messagebox.askyesno(
            "Xác nhận xoá", f"Xoá {len(sel)} job đã chọn khỏi danh sách?"
        ):
            return
        indices = sorted((self.tree.index(item) for item in sel), reverse=True)
        for item in sel:
            self.tree.delete(item)
        for idx in indices:
            del self.jobs[idx]
        self._refresh_jobs_view()

    def edit_job(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn job", "Hãy chọn 1 job trong danh sách trước.")
            return
        idx = self.tree.index(sel[0])
        job = self.jobs[idx]
        dialog_cls = AddTextJobDialog if job.get("mode") == "text" else AddJobDialog
        dialog = dialog_cls(self, initial=job)
        self.wait_window(dialog)
        if dialog.result:
            if job.get("output_group") and "output_group" not in dialog.result:
                dialog.result["output_group"] = job["output_group"]
            self.jobs[idx] = dialog.result
            self.tree.item(sel[0], values=self._job_row_values(dialog.result))

    def open_output_folder(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.output_dir))

    def choose_output_dir(self):
        chosen = filedialog.askdirectory(
            title="Chọn thư mục lưu ảnh kết quả",
            initialdir=str(self.output_dir if self.output_dir.exists() else BASE_DIR),
        )
        if not chosen:
            return
        self.output_dir = Path(chosen)
        core.save_output_dir(BASE_DIR, self.output_dir)
        self._update_output_dir_label()
        messagebox.showinfo("Đã lưu", f"Ảnh kết quả sẽ được lưu vào:\n{self.output_dir}")

    def _update_output_dir_label(self):
        text = str(self.output_dir)
        if len(text) > 40:
            text = "..." + text[-37:]
        self.output_dir_status.configure(text=f"📁 {text}")

    def manage_accounts(self):
        dialog = AccountsDialog(self)
        self.wait_window(dialog)
        self.accounts = core.load_accounts_file(BASE_DIR)
        self._update_accounts_label()

    def manage_muse_config(self):
        dialog = MuseConfigDialog(self)
        self.wait_window(dialog)
        if dialog.saved:
            self.muse_config = core.load_muse_config(BASE_DIR)

    def manage_muse_accounts(self):
        MuseAccountsDialog(self)

    def open_video_maker(self):
        VideoMakerDialog(self)

    def open_hook1_maker(self):
        HookOneDialog(self)

    def open_hook3_maker(self):
        HookThreeDialog(self)

    def manage_kol_templates(self):
        KolTemplatesDialog(self)

    def _update_accounts_label(self):
        n = len(self.accounts)
        total = len(core.read_account_entries(BASE_DIR))
        if n == 0:
            self.accounts_status.configure(text="1 tài khoản (mặc định)")
        elif n == total:
            self.accounts_status.configure(text=f"{n} tài khoản ChatGPT (đang bật)")
        else:
            self.accounts_status.configure(text=f"{n}/{total} tài khoản đang bật")

    # ---------- Log / run ----------
    def _append_log(self, text: str, tag=None) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                msg, tag = self.log_queue.get_nowait()
                self._append_log(msg, tag)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def run_all(self):
        self._start_run(list(self.jobs))

    def run_selected(self):
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tiến trình chạy, vui lòng đợi xong hoặc bấm Dừng.")
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Chưa chọn job", "Hãy chọn 1 hoặc nhiều job trong danh sách trước "
                                                  "(giữ Ctrl hoặc Shift để chọn nhiều).")
            return
        indices = [self.tree.index(item) for item in sel]
        jobs = [self.jobs[i] for i in indices]
        self._start_run(jobs)

    def stop_run(self):
        if not self.running:
            return
        self.stop_requested = True
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Đang dừng... (chờ task đang chạy xong)")
        self.log_queue.put(("⏹ Đã yêu cầu dừng, đang chờ các task đang chạy hoàn tất rồi dừng hẳn...", "warn"))

    def _start_run(self, jobs):
        if self.running:
            messagebox.showinfo("Đang chạy", "Đang có tiến trình chạy, vui lòng đợi xong.")
            return
        if not jobs:
            messagebox.showinfo("Chưa có job", "Hãy thêm hoặc chọn ít nhất 1 job trước khi chạy.")
            return
        needs_codex = any(job.get("backend", "codex") != "muse" for job in jobs)
        needs_muse = any(job.get("backend") == "muse" for job in jobs)
        if needs_codex and not self.codex_bin:
            messagebox.showerror("Thiếu Codex CLI", "Không tìm thấy lệnh 'codex'. Cài đặt trước khi chạy.")
            return
        if needs_muse and not self.muse_config:
            messagebox.showerror(
                "Thiếu cấu hình MUSE2API",
                "Có job đang chọn backend Muse2API nhưng chưa cấu hình URL/API Key. "
                "Bấm \"🅜 Cấu hình MUSE2API\" để thiết lập trước.",
            )
            return

        self.running = True
        self.stop_requested = False
        self.run_btn.configure(state="disabled")
        self.run_selected_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=100)
        self.status_var.set("Đang chuẩn bị...")
        self.job_progress = {}
        job_names = {j["name"] for j in jobs}
        for item in self.tree.get_children():
            if self.tree.set(item, "name") in job_names:
                self.tree.set(item, "status", "⏳")
        threading.Thread(target=self._run_worker, args=(jobs,), daemon=True).start()

    def _log(self, label: str, msg: str) -> None:
        tag = None
        low = msg.lower()
        if "lỗi" in low or "error" in low:
            tag = "err"
        elif "cảnh báo" in low:
            tag = "warn"
        elif "đã lưu" in low or "đã có sẵn" in low:
            tag = "ok"
        self.log_queue.put((f"[{time.strftime('%H:%M:%S')}] [{label}] {msg}", tag))

    def _run_worker(self, jobs):
        tasks = []
        for job in jobs:
            output_group = job.get("output_group")
            base_dir = job.get("output_dir") or self.output_dir
            out_dir = base_dir / (output_group or job["name"])
            try:
                if job.get("mode") == "text":
                    job_tasks = core.build_text_task(
                        job["name"], job["prompt"], out_dir, job.get("count", 1),
                        sample_image=job.get("sample_image"), backend=job.get("backend", "codex"),
                    )
                else:
                    job_tasks = core.build_tasks_from_folder(
                        job["name"], job["folder"], job["prompt"], out_dir
                    )
                for t in job_tasks:
                    t["backend"] = job.get("backend", "codex")
                    if output_group and len(job_tasks) > 1 and not t["dest_name"].startswith(f"{job['name']}_"):
                        # Nhiều job gom chung 1 thư mục kết quả -> thêm tên job vào đầu tên file
                        # để tránh trùng tên khi 1 job có nhiều ảnh biến thể.
                        t["dest_name"] = f"{job['name']}_{t['dest_name']}"
                tasks.extend(job_tasks)
            except Exception as exc:
                self._log(job["name"], f"LỖI khi chuẩn bị job: {exc}")

        if not tasks:
            self.log_queue.put(("Không có task nào để chạy.", "err"))
            self._finish_run([])
            return

        core.assign_accounts(tasks, self.accounts)
        max_workers = core.workers_for(len(tasks), self.accounts, default_workers=MAX_WORKERS)

        total = len(tasks)
        if self.accounts:
            self.log_queue.put(
                (f"Dùng {len(self.accounts)} tài khoản, chia việc round-robin.", "dim")
            )
        self.log_queue.put((f"Tổng {total} task sẽ chạy song song (tối đa {max_workers} cùng lúc).", "dim"))
        self.after(0, lambda: self.progress.configure(maximum=total, value=0))

        job_totals = {}
        for t in tasks:
            job_name = t["label"].split("/")[0]
            job_totals[job_name] = job_totals.get(job_name, 0) + 1
        self.job_progress = {name: {"total": n, "done": 0, "ok": 0} for name, n in job_totals.items()}
        for job_name, info in self.job_progress.items():
            self.after(0, lambda jn=job_name, inf=info: self._update_job_status_cell(jn, inf))

        results = []
        done_count = 0
        healthy_accounts = list(self.accounts)
        tried_accounts: dict[int, set] = {}
        reported_limited: set = set()

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            task_of = {
                pool.submit(core.run_task, self.codex_bin, t, self._log, False, self.muse_config): t
                for t in tasks
            }
            pending = set(task_of.keys())

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if self.stop_requested:
                    for p in pending:
                        p.cancel()
                for fut in done:
                    if fut.cancelled():
                        continue
                    t = task_of[fut]
                    try:
                        r = fut.result()
                    except Exception as exc:
                        self._log(t["label"], f"LỖI: {exc}")
                        r = {"label": t["label"], "ok": False, "images": [], "limited": False, "codex_home": None}

                    if r.get("limited") and not self.stop_requested and t.get("backend") == "muse":
                        self.log_queue.put((
                            "⚠️ MUSE có thể đã hết limit toàn bộ tài khoản, bỏ qua ảnh này "
                            "(bấm \"🅜 Tài khoản Muse\" để kiểm tra/bật tài khoản khác).", "warn",
                        ))
                    elif r.get("limited") and not self.stop_requested:
                        bad_acc = r.get("codex_home")
                        if bad_acc in healthy_accounts:
                            healthy_accounts.remove(bad_acc)
                            key = str(bad_acc)
                            if key not in reported_limited:
                                reported_limited.add(key)
                                self.log_queue.put((
                                    f"⚠️ Tài khoản '{core.account_label(bad_acc)}' có vẻ đã hết limit, "
                                    "tạm loại khỏi vòng xoay cho lượt chạy này.", "warn",
                                ))
                        tried = tried_accounts.setdefault(id(t), set())
                        if bad_acc is not None:
                            tried.add(bad_acc)
                        remaining = [a for a in healthy_accounts if a not in tried]
                        if remaining:
                            new_acc = remaining[0]
                            t["codex_home"] = new_acc
                            self._log(t["label"], f"🔁 Tự động chạy lại bằng tài khoản khác: "
                                                   f"{core.account_label(new_acc)}")
                            new_fut = pool.submit(core.run_task, self.codex_bin, t, self._log, False,
                                                   self.muse_config)
                            task_of[new_fut] = t
                            pending.add(new_fut)
                            continue
                        else:
                            self._log(t["label"], "❌ Không còn tài khoản nào khác khả dụng, bỏ qua ảnh này.")

                    results.append(r)
                    done_count += 1
                    self.after(0, lambda v=done_count: self.progress.configure(value=v))
                    self.after(0, lambda label=t["label"], ok=r["ok"]: self._mark_job_status(label, ok))

        if self.stop_requested:
            self.log_queue.put(("⏹ Đã dừng theo yêu cầu người dùng.", "warn"))
        self._finish_run(results)

    def _mark_job_status(self, label: str, ok: bool) -> None:
        job_name = label.split("/")[0]
        info = self.job_progress.get(job_name)
        if info is None:
            info = {"total": 1, "done": 0, "ok": 0}
            self.job_progress[job_name] = info
        info["done"] += 1
        if ok:
            info["ok"] += 1
        self._update_job_status_cell(job_name, info)

    def _update_job_status_cell(self, job_name: str, info: dict) -> None:
        done, total, ok = info["done"], info["total"], info["ok"]
        if done == 0:
            icon = "⏳"
        elif done < total:
            icon = "⏳"
        elif ok == total:
            icon = "✅"
        else:
            icon = "⚠️"
        text = f"{icon} {ok}/{total}" if done > 0 else f"{icon} 0/{total}"
        for item in self.tree.get_children():
            if self.tree.set(item, "name") == job_name:
                self.tree.set(item, "status", text)

    def _finish_run(self, results):
        self.log_queue.put(("", None))
        self.log_queue.put(("===== TỔNG KẾT =====", "dim"))
        ok_count = 0
        for r in results:
            if r["ok"]:
                ok_count += 1
                self.log_queue.put((f"✅ {r['label']}: OK ({len(r['images'])} ảnh)", "ok"))
            else:
                self.log_queue.put((f"⚠️ {r['label']}: LỖI", "err"))

        fail_count = len(results) - ok_count

        def done():
            self.running = False
            stopped = self.stop_requested
            self.stop_requested = False
            self.run_btn.configure(state="normal")
            self.run_selected_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            prefix = "Đã dừng. " if stopped else "Xong: "
            self.status_var.set(f"{prefix}{ok_count}/{len(results)} task thành công.")
            self._notify_finished(ok_count, fail_count, len(results))

        self.after(0, done)

    def _clear_taskbar_badge(self, event=None) -> None:
        try:
            set_taskbar_badge(self.winfo_id(), False)
        except Exception:
            pass

    def _notify_finished(self, ok_count: int, fail_count: int, total: int) -> None:
        if total == 0:
            return

        if fail_count == 0:
            title = "✅ Gen ảnh hoàn tất"
            body = f"Đã xong tất cả {total} task thành công."
        else:
            title = "⚠️ Gen ảnh hoàn tất (có lỗi)"
            body = f"Xong: {ok_count}/{total} thành công, {fail_count} task bị lỗi. Kiểm tra lại khung Tiến trình."

        # Âm thanh báo hiệu bằng giọng nói
        sound_path = SOUND_ERR if fail_count > 0 else SOUND_OK
        if playsound and sound_path.exists():
            _play_sound_async(sound_path)
        elif winsound:
            try:
                winsound.MessageBeep(winsound.MB_OK if fail_count == 0 else winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

        # Chấm đỏ trên icon taskbar nếu cửa sổ đang không được focus
        try:
            if self.focus_displayof() is None:
                hwnd = self.winfo_id()
                set_taskbar_badge(hwnd, True)
        except Exception:
            pass

        # Toast thông báo Windows (hiện cả khi cửa sổ bị thu nhỏ/không active)
        if win_toast_notify:
            try:
                win_toast_notify(title, body, duration="short")
            except Exception:
                pass

        # Làm nháy icon taskbar nếu cửa sổ không đang được focus
        try:
            if self.focus_displayof() is None:
                self.bell()
        except Exception:
            pass


if __name__ == "__main__":
    App().mainloop()
