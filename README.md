# Gen Image Tool

Tool gen ảnh hàng loạt qua Codex CLI / Muse2API (`gui.py` + `gen_images.py`), kèm
pipeline dựng video quảng cáo (`video_core.py` - Kiểu 1/2/3).

## Cài đặt sau khi clone

```powershell
git clone https://github.com/Viet2003-coder/codex-image-tool.git
cd codex-image-tool
.\setup.ps1
```

`setup.ps1` sẽ tự tạo virtualenv `.venv` và cài hết thư viện trong
`requirements.txt`. Sau đó chạy:

```powershell
.\run_gui.ps1
```

(hoặc `.\.venv\Scripts\python.exe gui.py`)

## Yêu cầu hệ thống (cài riêng, không qua pip)

- **Python 3.12+** (đã có sẵn trên máy, `setup.ps1` chỉ tạo venv dựa trên Python này).
- **ffmpeg + ffprobe** (dùng cho mọi tính năng tạo video): `winget install Gyan.FFmpeg`
- **Codex CLI** (nếu dùng backend gen ảnh "codex"): cài theo hướng dẫn của Codex,
  sau đó chạy `codex login` một lần để đăng nhập trước khi gen ảnh.
- **Muse2API** (nếu dùng backend "muse"): chạy server riêng ở
  `../muse2api` (xem `.env` trong project đó), tool gọi qua
  `http://127.0.0.1:18610` (hoặc theo cấu hình trong "🅜 Cấu hình Muse").

## Build lại file .exe

```powershell
.\.venv\Scripts\pyinstaller.exe GenImageToolGUI.spec --noconfirm
```

File `.exe` kết quả nằm ở `dist\GenImageToolGUI.exe`. `dist/` và `build/` không
được commit lên git (xem `.gitignore`) vì là file build tự tạo lại được.

## Cấu trúc chính

- `gui.py` - giao diện Tkinter/ttkbootstrap, quản lý job, tài khoản, video.
- `core.py` - logic gọi Codex/Muse2API, quản lý tài khoản, cấu hình.
- `gen_images.py` - chạy gen ảnh dạng CLI (không cần GUI).
- `video_core.py` - dựng video Kiểu 1/2/3 bằng ffmpeg.
- `assets/` - logo, nhạc, clip KOL mặc định dùng trong video.

Các file cấu hình/job sinh ra khi chạy (`*.json`, `accounts.txt`, `jobs/`) không
được commit vì chứa dữ liệu/tài khoản riêng của từng máy.
