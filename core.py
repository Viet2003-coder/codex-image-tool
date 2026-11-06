"""Logic dùng chung cho cả bản CLI (gen_images.py) và bản GUI (gui.py)."""

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
CODEX_HOME = Path.home() / ".codex"
GENERATED_IMAGES_DIR = CODEX_HOME / "generated_images"

ACCOUNTS_FILENAME = "accounts.txt"
PER_ACCOUNT_WORKERS = 7

MUSE_CONFIG_FILENAME = "muse_config.json"
OUTPUT_CONFIG_FILENAME = "output_config.json"
VIDEO_CONFIG_FILENAME = "video_config.json"
HOOK1_CONFIG_FILENAME = "hook1_config.json"
HOOK3_CONFIG_FILENAME = "hook3_config.json"
KOL_TEMPLATES_FILENAME = "kol_templates.json"


def find_codex_bin() -> str | None:
    return shutil.which("codex")


def _normalize_header(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.strip().lower()


NAME_HEADER_ALIASES = {"name", "ten", "tenjob", "ten job", "tên", "tên job"}
PROMPT_HEADER_ALIASES = {"prompt", "promt", "mo ta", "mô tả"}


def read_excel_jobs(path: Path) -> list[dict]:
    """Đọc file Excel (.xlsx) có cột 'name' và 'prompt' (không phân biệt hoa thường/dấu).

    Trả về list[{"name": str, "prompt": str}], bỏ qua dòng thiếu name hoặc prompt.
    Ném lỗi rõ ràng nếu không tìm thấy đủ 2 cột trên dòng tiêu đề (hàng đầu tiên).
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]

    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        raise ValueError("File Excel trống, không có dòng tiêu đề.")

    name_col = prompt_col = None
    for idx, cell in enumerate(header):
        if cell is None:
            continue
        normalized = _normalize_header(str(cell))
        if normalized in NAME_HEADER_ALIASES and name_col is None:
            name_col = idx
        elif normalized in PROMPT_HEADER_ALIASES and prompt_col is None:
            prompt_col = idx

    if name_col is None or prompt_col is None:
        raise ValueError(
            "Không tìm thấy đủ cột 'name' và 'prompt' ở dòng tiêu đề đầu tiên. "
            f"Dòng tiêu đề hiện tại: {list(header)}"
        )

    jobs = []
    for row in rows:
        if row is None or max(len(row), name_col + 1, prompt_col + 1) == 0:
            continue
        name = row[name_col] if name_col < len(row) else None
        prompt = row[prompt_col] if prompt_col < len(row) else None
        name = str(name).strip() if name is not None else ""
        prompt = str(prompt).strip() if prompt is not None else ""
        if not name or not prompt:
            continue
        jobs.append({"name": name, "prompt": prompt})

    return jobs


def read_account_entries(base_dir: Path) -> list[tuple[Path, bool]]:
    """Đọc TOÀN BỘ tài khoản đã khai báo trong accounts.txt, kèm trạng thái bật/tắt.

    Dòng bắt đầu bằng "#" = tài khoản đang TẮT (vẫn giữ trong danh sách, chỉ không dùng
    khi chạy gen ảnh). Dòng bình thường = đang BẬT.
    """
    path = base_dir / ACCOUNTS_FILENAME
    if not path.exists():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        active = not line.startswith("#")
        raw = line[1:].strip() if not active else line
        if not raw:
            continue
        entries.append((Path(raw).expanduser(), active))
    return entries


def write_account_entries(base_dir: Path, entries: list[tuple[Path, bool]]) -> None:
    lines = [f"{p}" if active else f"#{p}" for p, active in entries]
    text = "\n".join(lines) + ("\n" if lines else "")
    (base_dir / ACCOUNTS_FILENAME).write_text(text, encoding="utf-8")


def load_accounts_file(base_dir: Path) -> list[Path]:
    """Đọc danh sách tài khoản ĐANG BẬT (mỗi dòng = 1 thư mục CODEX_HOME) từ accounts.txt.

    Nếu file không tồn tại hoặc không có tài khoản nào đang bật -> trả về []
    (dùng tài khoản mặc định của máy).
    """
    return [p for p, active in read_account_entries(base_dir) if active]


def account_label(codex_home: Path | None) -> str:
    return codex_home.name if codex_home else "default"


LIMIT_ERROR_PATTERNS = (
    "usage limit", "rate limit", "rate_limit", "too many requests",
    "429", "quota", "limit reached", "exceeded your", "exceeded current quota",
    "resource_exhausted", "hết giới hạn", "hết hạn mức", "vượt giới hạn", "hết lượt",
    "没有可用账号", "账号已用尽", "配额",
)


def is_limit_message(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in LIMIT_ERROR_PATTERNS)


def assign_accounts(tasks: list[dict], accounts: list[Path]) -> None:
    """Gán round-robin 1 CODEX_HOME (tài khoản) cho mỗi task, nếu có khai báo accounts."""
    if not accounts:
        return
    for i, task in enumerate(tasks):
        task["codex_home"] = accounts[i % len(accounts)]


def workers_for(total_tasks: int, accounts: list[Path], default_workers: int) -> int:
    if accounts:
        return max(1, min(total_tasks, len(accounts) * PER_ACCOUNT_WORKERS))
    return max(1, min(total_tasks, default_workers))


def collect_job_images(job_dir: Path) -> tuple[Path | None, list[Path]]:
    """Lấy ảnh trong 1 thư mục job.

    Nếu có file sample.* -> dùng làm ảnh gốc chung, các ảnh còn lại là biến thể
    (mỗi biến thể sẽ gen cùng với sample). Nếu KHÔNG có sample.* -> mọi ảnh trong
    thư mục đều là ảnh biến thể độc lập (mỗi ảnh tự gen riêng với prompt, không cần ảnh gốc).
    """
    sample = job_dir / "sample.png"
    if not sample.exists():
        candidates = sorted(
            p for p in job_dir.glob("sample.*") if p.suffix.lower() in IMAGE_EXTS
        )
        sample = candidates[0] if candidates else None

    all_images = sorted(
        p for p in job_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS
    )
    if sample is not None:
        variants = [p for p in all_images if p.resolve() != sample.resolve()]
    else:
        variants = all_images
        if not variants:
            raise FileNotFoundError(f"Không tìm thấy ảnh nào trong {job_dir}")
    return sample, variants


def call_codex(
    codex_bin: str,
    images: list[Path],
    prompt: str,
    on_log=None,
    codex_home: Path | None = None,
) -> tuple[list[Path], bool, str]:
    """Chạy 1 lượt codex exec, trả về (danh sách file ảnh đã gen, có phải lỗi hết limit?, chi tiết lỗi).

    Nếu codex_home được chỉ định, lệnh sẽ dùng tài khoản đăng nhập trong thư mục đó
    (cho phép chạy nhiều tài khoản ChatGPT khác nhau song song).
    """
    cmd = [codex_bin, "exec", "--skip-git-repo-check", "--json", "-"]
    for img in images:
        cmd += ["-i", str(img)]

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        env=env,
    )

    thread_id = None
    last_agent_messages = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    last_agent_messages.append(text)

    last_msg = last_agent_messages[-1] if last_agent_messages else ""
    stderr_tail = proc.stderr.strip()[-800:]
    limited = is_limit_message(last_msg) or is_limit_message(stderr_tail)

    if not thread_id:
        if on_log:
            on_log("LỖI: không lấy được thread_id từ Codex.")
            if stderr_tail:
                on_log(stderr_tail)
        return [], limited, (stderr_tail or last_msg)

    generated_dir = (codex_home or CODEX_HOME) / "generated_images"
    src_dir = generated_dir / thread_id
    produced = sorted(src_dir.glob("*.png")) if src_dir.exists() else []

    if not produced:
        if on_log and last_msg:
            on_log(f"Codex trả lời: {last_msg[:500]}")
        limited = limited or is_limit_message(last_msg)

    return produced, limited, last_msg


def build_text_task(
    job_name: str, prompt: str, out_dir: Path, count: int = 1,
    sample_image: Path | None = None, backend: str = "codex",
) -> list[dict]:
    """Tạo task từ prompt. Nếu có sample_image (tuỳ chọn), ảnh đó sẽ được đính kèm gửi
    cùng prompt (Codex/Muse dùng làm ảnh tham khảo); nếu không, tự vẽ ảnh mới hoàn toàn
    chỉ dựa vào mô tả.

    count > 1: tách thành `count` TASK RIÊNG, mỗi task gọi Codex/Muse2API 1 lần với
    cùng 1 prompt (dest_name đánh số _01, _02...). Trước đây với Codex có thử gộp
    chung 1 lượt gọi kèm yêu cầu "tạo N ảnh biến thể" trong prompt, nhưng model không
    tuân thủ ổn định (có lúc chỉ trả về đúng 1 ảnh) nên đổi về tách N lượt gọi riêng
    cho chắc chắn ra đủ N ảnh, giống cách Muse2API đã làm (Muse2API thì bắt buộc phải
    tách vì tham số "n" của API không được hỗ trợ, luôn chỉ trả 1 ảnh/lượt gọi).
    """
    images = [sample_image] if sample_image else []

    if count > 1:
        return [
            {
                "label": f"{job_name}/{i:02d}",
                "out_dir": out_dir,
                "images": images,
                "prompt": prompt,
                "dest_name": f"{job_name}_{i:02d}.png",
            }
            for i in range(1, count + 1)
        ]

    return [{
        "label": job_name,
        "out_dir": out_dir,
        "images": images,
        "prompt": prompt,
        "dest_name": f"{job_name}.png",
    }]


def build_tasks_from_folder(job_name: str, job_dir: Path, prompt: str, out_dir: Path) -> list[dict]:
    """Tạo danh sách task từ 1 thư mục ảnh + 1 prompt.

    Có sample.* -> mỗi biến thể gen chung với sample (1 task/variant, images=[sample, variant]).
    Không có sample.* -> mỗi ảnh trong thư mục là 1 biến thể độc lập, tự gen riêng với prompt
    (1 task/ảnh, images=[ảnh đó]).
    """
    sample, variants = collect_job_images(job_dir)

    tasks = []
    if sample is not None:
        if variants:
            for variant in variants:
                tasks.append({
                    "label": f"{job_name}/{variant.stem}",
                    "out_dir": out_dir,
                    "images": [sample, variant],
                    "prompt": prompt,
                    "dest_name": f"{variant.stem}.png",
                })
        else:
            tasks.append({
                "label": job_name,
                "out_dir": out_dir,
                "images": [sample],
                "prompt": prompt,
                "dest_name": f"{job_name}.png",
            })
    else:
        for variant in variants:
            tasks.append({
                "label": f"{job_name}/{variant.stem}",
                "out_dir": out_dir,
                "images": [variant],
                "prompt": prompt,
                "dest_name": f"{variant.stem}.png",
            })
    return tasks


def run_task(codex_bin: str, task: dict, on_log=None, force: bool = False, muse_config: dict | None = None) -> dict:
    out_dir = task["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    label = task["label"]

    def log(msg: str) -> None:
        if on_log:
            on_log(label, msg)

    base_name = task["dest_name"]
    expected_single = base_name if base_name.lower().endswith(".png") else f"{base_name}.png"
    expected_path = out_dir / expected_single
    if not force and expected_path.exists():
        log(f"Đã có sẵn, bỏ qua: {expected_path}")
        return {"label": label, "ok": True, "images": [str(expected_path)], "skipped": True}

    backend = task.get("backend", "codex")
    codex_home = None
    limited = False
    if backend == "muse":
        if not muse_config:
            log("LỖI: chưa cấu hình MUSE2API (Base URL / API Key).")
            return {"label": label, "ok": False, "images": [], "limited": False, "codex_home": None}
        log(f"Bắt đầu [MUSE2API]: images={[p.name for p in task['images']]}")
        produced, limited, detail = call_muse_images(
            muse_config["base_url"], muse_config["api_key"], task["images"], task["prompt"],
            on_log=lambda m: log(m),
        )
        if not produced and detail:
            tag = "⚠️ MUSE CÓ THỂ ĐÃ HẾT LIMIT" if limited else "LỖI MUSE"
            extra = " (mở \"🅜 Tài khoản Muse\" để kiểm tra/bật tài khoản khác)." if limited else ""
            log(f"{tag}: {detail[:400]}{extra}")
    else:
        codex_home = task.get("codex_home")
        acc_tag = f" [tài khoản: {account_label(codex_home)}]" if codex_home else ""
        log(f"Bắt đầu{acc_tag}: images={[p.name for p in task['images']]}")
        produced, limited, detail = call_codex(
            codex_bin, task["images"], task["prompt"], on_log=lambda m: log(m), codex_home=codex_home
        )
        if not produced:
            if limited:
                log(f"⚠️ CÓ THỂ ĐÃ HẾT LIMIT (tài khoản: {account_label(codex_home)}): {detail[:300]}")
            elif detail:
                log(f"❌ LỖI GEN ẢNH (tài khoản: {account_label(codex_home)}): {detail[:300]}")
            else:
                log(f"❌ LỖI GEN ẢNH (tài khoản: {account_label(codex_home)}): không rõ lý do "
                    f"(Codex không trả về ảnh nào).")

    saved = []
    for idx, item in enumerate(produced, start=1):
        if len(produced) > 1:
            stem = base_name[:-4] if base_name.lower().endswith(".png") else base_name
            dest_name = f"{stem}_{idx:02d}.png"
        else:
            dest_name = base_name if base_name.lower().endswith(".png") else f"{base_name}.png"
        dest = out_dir / dest_name
        if isinstance(item, (bytes, bytearray)):
            dest.write_bytes(item)
        else:
            shutil.copyfile(item, dest)
        saved.append(str(dest))
        log(f"Đã lưu: {dest}")

    if not saved:
        log("CẢNH BÁO: không thấy ảnh nào được tạo.")

    return {
        "label": label, "ok": bool(saved), "images": saved,
        "limited": bool(limited and not saved), "codex_home": codex_home if backend == "codex" else None,
    }


def load_muse_config(base_dir: Path) -> dict | None:
    path = base_dir / MUSE_CONFIG_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data.get("base_url") or not data.get("api_key"):
        return None
    return data


def save_muse_config(base_dir: Path, base_url: str, api_key: str) -> None:
    data = {"base_url": base_url.rstrip("/"), "api_key": api_key}
    (base_dir / MUSE_CONFIG_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_muse_accounts(base_url: str, api_key: str, timeout: int = 15) -> list[dict]:
    """Lấy danh sách tài khoản Muse (do Muse2API quản lý nội bộ) qua API admin.

    Mỗi tài khoản gồm: id, label, enabled, ok, note, use_count, quota (dict có
    weekly_used_pct...). Ném exception nếu không gọi được (vd sai API key, server
    Muse2API chưa chạy).
    """
    import requests

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(f"{base_url}/admin/accounts", headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("accounts", [])


def set_muse_account_enabled(base_url: str, api_key: str, account_id: str, enabled: bool,
                              timeout: int = 15) -> dict:
    """Bật/tắt 1 tài khoản Muse qua API admin (PATCH /admin/accounts/{id})."""
    import requests

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.patch(
        f"{base_url}/admin/accounts/{account_id}", headers=headers,
        json={"enabled": enabled}, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# Muse2API (engine.py) trả lỗi bằng tiếng Trung - dịch sẵn các câu thường gặp sang
# tiếng Việt cho dễ đọc. Khớp theo thứ tự (câu dài/cụ thể trước) rồi thay thế.
MUSE_ERROR_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("没有可用账号，请先在管理页面导入 cookie", "Không có tài khoản Muse nào khả dụng, hãy vào trang quản trị MUSE2API import cookie trước"),
    ("没有可用账号", "Không có tài khoản Muse nào khả dụng"),
    ("等待空闲账号超时，当前所有账号均处于繁忙中", "Hết thời gian chờ tài khoản rảnh, tất cả tài khoản Muse hiện đang bận"),
    ("等待浏览器队列超时，请稍后重试", "Hết thời gian chờ hàng đợi trình duyệt, vui lòng thử lại sau"),
    ("任务总等待时限已到，停止重试", "Đã hết tổng thời gian chờ, dừng thử lại"),
    ("账号额度不足", "Tài khoản Muse đã hết hạn mức (quota)"),
    ("积分不足", "không đủ điểm (credit)"),
    ("模型未生成媒体，仅返回文本", "Model không tạo ra ảnh/video, chỉ trả lời bằng văn bản"),
    ("提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试",
     "Prompt chưa gửi thành công tới server (Still sending / Delivery not confirmed), dừng chờ và thử lại"),
    ("Muse 未建立视频任务线程，页面在提交结束后持续无响应（已提前停止等待）",
     "Muse chưa tạo được luồng xử lý video, trang không phản hồi sau khi gửi (đã dừng chờ sớm)"),
    ("等待生成超时，未出现新的生成结果", "Hết thời gian chờ tạo ảnh/video, không thấy kết quả mới"),
    ("期望生成视频，但提取成品类型为图片", "Yêu cầu tạo video nhưng kết quả lấy được lại là ảnh"),
    ("拒绝以图片冒充视频交付", "nên từ chối trả về ảnh thay cho video"),
    ("任务准备超时，已停止发送", "Hết thời gian chuẩn bị tác vụ, đã dừng gửi"),
    ("提示词发送未确认，已停止生成", "Gửi prompt chưa được xác nhận, đã dừng tạo"),
    ("任务已取消", "Tác vụ đã bị huỷ"),
    ("客户端已断开连接，终止生成任务", "Client đã ngắt kết nối, dừng tác vụ tạo ảnh"),
    ("找不到聊天输入框", "Không tìm thấy ô nhập chat trên trang muse.ai"),
    ("会话认证失败", "Xác thực phiên đăng nhập thất bại"),
    ("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控）", "Phiên đăng nhập đã bị muse.ai đăng xuất (có thể do đăng nhập nơi khác hoặc bị khoá tạm)"),
    ("muse.ai 页面加载超时（未出现聊天输入框），请检查服务器网络后重试；未确认会话失效",
     "Trang muse.ai tải quá thời gian chờ (không thấy ô nhập chat), hãy kiểm tra mạng server rồi thử lại; chưa chắc phiên đăng nhập đã hỏng"),
    ("页面网关连接超时 (Connecting...)，正在自动切换账号", "Kết nối cổng trang quá thời gian chờ (Connecting...), đang tự động đổi tài khoản"),
    ("账号代理认证失败", "Xác thực proxy của tài khoản thất bại"),
    ("账号代理浏览器连接失败，请检查节点或认证；未回退直连", "Trình duyệt kết nối proxy tài khoản thất bại, hãy kiểm tra node/thông tin xác thực proxy"),
    ("云端 VM 连接超时 (Still sending)", "Kết nối VM đám mây quá thời gian chờ (Still sending)"),
    ("等待助手首字响应超时", "Hết thời gian chờ ký tự phản hồi đầu tiên từ model"),
    ("等待助手回复超时", "Hết thời gian chờ phản hồi từ model"),
    ("未能取回生成结果", "Không lấy được kết quả đã tạo"),
    ("Chromium 启动失败", "Khởi động trình duyệt Chromium thất bại"),
    ("账号不存在", "Tài khoản không tồn tại"),
    ("账号正在执行任务或检测，请稍后再试", "Tài khoản đang bận chạy tác vụ khác, vui lòng thử lại sau"),
    ("会话正常", "Phiên đăng nhập bình thường"),
    ("代理已修改，请重新测试", "Proxy đã được sửa, hãy kiểm tra lại"),
    ("代理可连接，但 Muse 会话认证失败，请重新导入 Cookie", "Proxy kết nối được, nhưng xác thực phiên Muse thất bại, hãy import lại Cookie"),
    ("已更新 cookie", "Đã cập nhật cookie"),
)


def translate_muse_error(text: str) -> str:
    """Dịch các câu lỗi tiếng Trung thường gặp từ Muse2API sang tiếng Việt cho dễ đọc.
    Chỉ thay thế các cụm đã biết, phần còn lại (nếu có) giữ nguyên."""
    if not text:
        return text
    out = text
    for cn, vi in MUSE_ERROR_TRANSLATIONS:
        out = out.replace(cn, vi)
    return out


def muse_account_is_limited(acc: dict) -> bool:
    """Tài khoản Muse có vẻ đã hết limit/lỗi: ok=False, hoặc note nhắc tới quota/limit,
    hoặc đã dùng hết 100% hạn mức tuần và không còn token phụ."""
    if acc.get("ok") is False:
        return True
    if is_limit_message(acc.get("note") or ""):
        return True
    quota = acc.get("quota") or {}
    if quota.get("weekly_used_pct", 0) >= 100 and quota.get("extra_used_pct", 0) >= 100:
        return True
    return False


def muse_account_label(acc: dict) -> str:
    return acc.get("label") or acc.get("id") or "?"


def load_output_dir(base_dir: Path) -> Path | None:
    path = base_dir / OUTPUT_CONFIG_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    folder = data.get("output_dir")
    if not folder:
        return None
    return Path(folder)


def save_output_dir(base_dir: Path, output_dir: Path | str) -> None:
    data = {"output_dir": str(output_dir)}
    (base_dir / OUTPUT_CONFIG_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_video_config(base_dir: Path) -> dict:
    path = base_dir / VIDEO_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_video_config(base_dir: Path, **fields) -> None:
    data = load_video_config(base_dir)
    data.update({k: v for k, v in fields.items() if v is not None})
    (base_dir / VIDEO_CONFIG_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_hook1_config(base_dir: Path) -> dict:
    path = base_dir / HOOK1_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hook1_config(base_dir: Path, **fields) -> None:
    data = load_hook1_config(base_dir)
    data.update({k: v for k, v in fields.items() if v is not None})
    (base_dir / HOOK1_CONFIG_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_hook3_config(base_dir: Path) -> dict:
    path = base_dir / HOOK3_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hook3_config(base_dir: Path, **fields) -> None:
    data = load_hook3_config(base_dir)
    data.update({k: v for k, v in fields.items() if v is not None})
    (base_dir / HOOK3_CONFIG_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_kol_templates(base_dir: Path) -> list[dict]:
    """Danh sách clip KOL (mỗi clip: name, video, box=[x,y,w,h], corners=[tl,tr,br,bl],
    radius) đã lưu, dùng cho đoạn "KOL chỉ tay vào ảnh" (Hook 3)."""
    path = base_dir / KOL_TEMPLATES_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_kol_templates(base_dir: Path, templates: list[dict]) -> None:
    (base_dir / KOL_TEMPLATES_FILENAME).write_text(
        json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def call_muse_images(
    base_url: str, api_key: str, images: list[Path], prompt: str, on_log=None, timeout: int = 240,
) -> tuple[list[bytes], bool, str]:
    """Gọi MUSE2API: /v1/images/edits nếu có ảnh tham khảo, /v1/images/generations nếu không.

    Trả về (list[bytes] ảnh kết quả, limited, detail):
    - limited=True nếu lỗi có vẻ do hết limit/quota (toàn bộ tài khoản Muse2API đang bận/hết hạn mức).
    - detail: chi tiết lỗi (nếu có) để log.
    """
    import requests

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        if images:
            file_handles = [open(img, "rb") for img in images]
            try:
                files = [
                    ("image", (img.name, fh, "application/octet-stream"))
                    for img, fh in zip(images, file_handles)
                ]
                resp = requests.post(
                    f"{base_url}/v1/images/edits",
                    headers=headers,
                    data={"prompt": prompt},
                    files=files,
                    timeout=timeout,
                )
            finally:
                for fh in file_handles:
                    fh.close()
        else:
            resp = requests.post(
                f"{base_url}/v1/images/generations",
                headers=headers,
                json={"model": "muse-image", "prompt": prompt, "response_format": "url"},
                timeout=timeout,
            )
    except Exception as exc:
        msg = f"LỖI kết nối MUSE2API: {exc}"
        if on_log:
            on_log(msg)
        return [], False, msg

    if resp.status_code >= 400:
        detail = resp.text[:500]
        try:
            err_obj = resp.json().get("error") or {}
            if err_obj.get("message"):
                detail = err_obj["message"]
        except Exception:
            pass
        limited = resp.status_code == 429 or is_limit_message(detail)
        detail = translate_muse_error(detail)
        if on_log:
            on_log(f"MUSE2API trả lỗi HTTP {resp.status_code}: {detail}")
        return [], limited, detail

    try:
        data = resp.json().get("data", [])
    except Exception as exc:
        msg = f"LỖI đọc phản hồi MUSE2API: {exc}"
        if on_log:
            on_log(msg)
        return [], False, msg

    results = []
    for item in data:
        try:
            if item.get("b64_json"):
                import base64
                raw = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                url = item["url"]
                if url.startswith("/"):
                    url = base_url + url
                img_resp = requests.get(url, headers=headers, timeout=timeout)
                img_resp.raise_for_status()
                raw = img_resp.content
            else:
                continue
            results.append(_to_real_png(raw, on_log=on_log))
        except Exception as exc:
            if on_log:
                on_log(f"LỖI tải ảnh kết quả từ MUSE2API: {exc}")

    if not results and on_log:
        on_log("MUSE2API không trả về ảnh nào (có thể prompt bị từ chối hoặc tài khoản hết hạn mức).")

    return results, False, ""


def _to_real_png(raw: bytes, on_log=None) -> bytes:
    """MUSE2API thường trả ảnh dạng WEBP dù tool luôn đặt tên file .png - ép chuyển
    đổi thật sự sang PNG để các trình xem ảnh (Windows Photos...) mở được bình thường.
    """
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        if on_log:
            on_log(f"CẢNH BÁO: không chuyển đổi được ảnh sang PNG ({exc}), lưu nguyên dữ liệu gốc.")
        return raw


def test_muse_connection(base_url: str, api_key: str, timeout: int = 30) -> tuple[bool, str]:
    """Gửi 1 tin nhắn chat ngắn để kiểm tra kết nối + API key + có tài khoản hoạt động."""
    import requests

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            headers=headers,
            json={
                "model": "muse-spark",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
        )
    except Exception as exc:
        return False, f"Không kết nối được: {exc}"

    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    return True, "Kết nối OK, MUSE2API phản hồi thành công."
