"""Gen Image Tool (bản dòng lệnh) - gen ảnh hàng loạt bằng Codex CLI.

Cách dùng: xem HUONG_DAN_SU_DUNG.txt
"""

import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import core

BASE_DIR = Path(sys.argv[0]).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
OUTPUT_DIR = core.load_output_dir(BASE_DIR) or (BASE_DIR / "output")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODEX_BIN = core.find_codex_bin()
if not CODEX_BIN:
    print("Không tìm thấy lệnh 'codex' trong PATH. Hãy cài Codex CLI và đăng nhập trước (codex login).")
    sys.exit(1)


def log(label: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{label}] {msg}", flush=True)


def read_optional(job_dir: Path, filename: str, default: str) -> str:
    f = job_dir / filename
    if f.exists():
        return f.read_text(encoding="utf-8").strip() or default
    return default


def build_tasks(job_dir: Path) -> list[dict]:
    job_name = job_dir.name
    out_dir = OUTPUT_DIR / job_name

    prompt_file = job_dir / "prompt.txt"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Thiếu prompt.txt trong {job_dir}")
    user_prompt = prompt_file.read_text(encoding="utf-8").strip()

    sample, variants = core.collect_job_images(job_dir)
    if not variants:
        count = int(read_optional(job_dir, "count.txt", "1") or "1")
        name_prefix = read_optional(job_dir, "name.txt", job_name)
        user_prompt = f"{user_prompt}\n\nHãy tạo đúng {count} ảnh (nếu được phép tạo nhiều ảnh trong 1 lượt)."
        return [{
            "label": job_name,
            "out_dir": out_dir,
            "images": [sample],
            "prompt": user_prompt,
            "dest_name": name_prefix,
        }]

    return core.build_tasks_from_folder(job_name, job_dir, user_prompt, out_dir)


def main() -> None:
    if not JOBS_DIR.exists():
        print(f"Không tìm thấy thư mục jobs/ tại {JOBS_DIR}")
        return

    job_dirs = [d for d in JOBS_DIR.iterdir() if d.is_dir()]
    if not job_dirs:
        print(f"Không có job nào trong {JOBS_DIR}. Hãy tạo thư mục con cho mỗi bộ ảnh.")
        return

    tasks = []
    for d in job_dirs:
        try:
            tasks.extend(build_tasks(d))
        except Exception as exc:
            log(d.name, f"LỖI khi chuẩn bị job: {exc}")

    if not tasks:
        print("Không có task nào để chạy.")
        return

    accounts = core.load_accounts_file(BASE_DIR)
    core.assign_accounts(tasks, accounts)
    max_workers = core.workers_for(len(tasks), accounts, default_workers=7)

    if accounts:
        print(f"Phát hiện {len(accounts)} tài khoản trong accounts.txt - chia việc round-robin.")
    print(f"Tìm thấy {len(job_dirs)} job, tổng {len(tasks)} task sẽ chạy song song (tối đa {max_workers} cùng lúc).")
    muse_config = core.load_muse_config(BASE_DIR)
    results = []
    healthy_accounts = list(accounts)
    tried_accounts: dict[int, set] = {}
    reported_limited: set = set()
    MAX_ATTEMPTS = 3
    attempt_count: dict[int, int] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        task_of = {
            pool.submit(core.run_task, CODEX_BIN, t, log, False, muse_config): t for t in tasks
        }
        pending = set(task_of.keys())

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                t = task_of[fut]
                try:
                    r = fut.result()
                except Exception as exc:
                    log(t["label"], f"LỖI: {exc}")
                    r = {"label": t["label"], "ok": False, "images": [], "limited": False, "codex_home": None}

                if not r.get("ok"):
                    task_key = id(t)
                    attempts = attempt_count.get(task_key, 1)
                    exhausted_accounts = False
                    retry_acc = None

                    if r.get("limited") and t.get("backend") == "muse":
                        log(t["label"], "⚠️ MUSE có thể đã hết limit toàn bộ tài khoản, sẽ tự thử lại "
                                         "(mở trình quản lý tài khoản Muse để kiểm tra/bật tài khoản khác).")
                    elif r.get("limited"):
                        bad_acc = r.get("codex_home")
                        if bad_acc in healthy_accounts:
                            healthy_accounts.remove(bad_acc)
                            rkey = str(bad_acc)
                            if rkey not in reported_limited:
                                reported_limited.add(rkey)
                                log(t["label"], f"⚠️ Tài khoản '{core.account_label(bad_acc)}' có vẻ đã hết limit, "
                                                "tạm loại khỏi vòng xoay cho lượt chạy này.")
                        tried = tried_accounts.setdefault(task_key, set())
                        if bad_acc is not None:
                            tried.add(bad_acc)
                        remaining = [a for a in healthy_accounts if a not in tried]
                        if remaining:
                            retry_acc = remaining[0]
                        else:
                            exhausted_accounts = True

                    if exhausted_accounts:
                        log(t["label"], "❌ Không còn tài khoản nào khác khả dụng, bỏ qua ảnh này.")
                    elif attempts < MAX_ATTEMPTS:
                        attempt_count[task_key] = attempts + 1
                        if retry_acc is not None:
                            t["codex_home"] = retry_acc
                            log(t["label"], f"🔁 Tự động chạy lại (lần {attempts + 1}/{MAX_ATTEMPTS}) "
                                            f"bằng tài khoản khác: {core.account_label(retry_acc)}")
                        else:
                            log(t["label"], f"🔁 Tự động chạy lại (lần {attempts + 1}/{MAX_ATTEMPTS})...")
                        new_fut = pool.submit(core.run_task, CODEX_BIN, t, log, False, muse_config)
                        task_of[new_fut] = t
                        pending.add(new_fut)
                        continue
                    else:
                        log(t["label"], f"❌ Bỏ qua sau {MAX_ATTEMPTS} lần lỗi liên tiếp.")

                results.append(r)

    print("\n===== TỔNG KẾT =====")
    for r in results:
        status = "OK" if r["ok"] else "LỖI"
        print(f"- {r['label']}: {status} ({len(r['images'])} ảnh)")


if __name__ == "__main__":
    main()
