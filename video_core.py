"""Tạo video demo dạng: ảnh #1 -(kéo trái)-> ảnh #2 -> màn "tải ảnh lên" (ảnh user
trượt vào khung) -(phủ nền tối)-> replay lại ảnh #1 -(kéo trái)-> ảnh #2.

Chạy bằng ffmpeg (subprocess) + Pillow, dùng đúng ảnh asset
assets/UPLOAD_YOUR_PHOTO_1.jpg làm nền khung upload.
"""

import math
import random
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Dùng sys.argv[0] (thư mục chứa file .exe/.py thật) thay vì __file__, vì khi đóng gói
# bằng PyInstaller, __file__ trỏ vào thư mục tạm giải nén (_MEI...), không phải nơi
# đặt file .exe - khiến không tìm thấy assets/ nằm cạnh .exe.
ASSETS_DIR = Path(sys.argv[0]).resolve().parent / "assets"
UPLOAD_REFERENCE_PATH = ASSETS_DIR / "UPLOAD_YOUR_PHOTO_1.jpg"
DEFAULT_KOL_VIDEO = ASSETS_DIR / "kol_reaction_raw.mp4"
# Khung chứa ảnh trong assets/kol_reaction_raw.mp4 (clip 1080x1920, 2s, đã cắt sẵn từ
# video mẫu preview_006b.mp4): khối chữ nhật góc dưới-trái, chỉ bo tròn góc TRÊN-PHẢI,
# 3 cạnh còn lại chạy sát viền màn hình. Đo trực tiếp trên khung hình của clip này.
KOL_BOX = (0, 955, 580, 965)  # x, y, w, h
KOL_BOX_RADIUS = 40

W, H = 1080, 1920
FPS = 30

SEG_DUR = 3.0       # thời lượng mỗi ảnh trend
UPLOAD_DUR = 2.0     # thời lượng đoạn "tải ảnh lên" (loading chỉ ~1-1.5s sau khi ảnh vào khung)
XFADE = 0.6          # thời lượng chuyển cảnh mờ dần giữa 2 đoạn (mượt hơn)
SLIDE_T = 0.35       # thời gian ảnh user trượt lên khớp khung (nhanh hơn)
RING_SIZE = 110      # kích thước khung chứa vòng xoay (hình vuông, để xoay không bị cắt)
RING_SPEED = 1.3     # vòng/giây của vòng xoay "Generating..."

SLIDE_START = 0.3    # ảnh user bắt đầu trượt lên sau khi khung upload hiện được 1 chút

# Ngay khi ảnh trượt vào khung xong là loading (thẻ "Generating..." + vòng xoay) hiện
# luôn, và lớp phủ ngả xám + tối cũng bắt đầu từ đó, giữ nguyên liên tục cho tới khi
# cắt cảnh sang ảnh kết quả tiếp theo (không chỉ tối trong vài giây cuối như trước).
DIM_RAMP = 0.5       # thời gian tăng dần tới mức tối/ngả xám tối đa (sau đó giữ nguyên)
DIM_MAX = 0.18       # độ tối tối đa (eq brightness, -1..1) - chỉ hơi mờ chứ không đen hẳn
SAT_MIN = 0.15        # độ bão hoà màu tối thiểu (càng nhỏ càng ngả xám)

BOX_RADIUS = 45  # bo góc vùng khung ảnh + ảnh user (áp dụng chung cho mọi khung, kể cả khung tuỳ chọn)

HEADER_TEXT = "UPLOAD YOUR PHOTO"
GENERATING_TEXT = "Generating..."

# Màu gradient đo trực tiếp từ nền pill chứa chữ "UPLOAD YOUR PHOTO" trong
# assets/UPLOAD_YOUR_PHOTO_1.jpg (cam san hô -> tím), dùng lại cho chữ + vòng xoay "Generating...".
HEADER_GRADIENT_START = (250, 87, 96)   # cam san hô
HEADER_GRADIENT_END = (144, 82, 249)    # tím

CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def _script_font(size: int) -> ImageFont.FreeTypeFont:
    """Font kiểu viết tay (Segoe Script, có sẵn trên Windows) cho chữ "Before"."""
    candidates = [
        r"C:\Windows\Fonts\segoescb.ttf",
        r"C:\Windows\Fonts\segoesc.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return _font(size)


def _lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _draw_arrow_curve(draw: ImageDraw.ImageDraw, p0, p1, p2, color=(255, 255, 255, 255),
                       width: int = 6, head_size: int = 22) -> None:
    """Vẽ 1 mũi tên CONG mượt từ p0 -> p2, uốn qua điểm điều khiển p1 (Bezier bậc 2),
    có đầu mũi tên ở p2 - dùng cho mũi tên chỉ từ chữ "Before" vào khung ảnh gốc."""
    steps = 40
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")

    dx, dy = pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]
    ang = math.atan2(dy, dx)
    tip = pts[-1]
    left = (tip[0] - head_size * math.cos(ang - 0.5), tip[1] - head_size * math.sin(ang - 0.5))
    right = (tip[0] - head_size * math.cos(ang + 0.5), tip[1] - head_size * math.sin(ang + 0.5))
    draw.polygon([tip, left, right], fill=color)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def _rounded_mask_top_right(size, radius):
    """Mask chữ nhật chỉ bo góc TRÊN-PHẢI (3 góc còn lại vuông) - dùng cho khung ảnh
    trong clip KOL (góc trên-phải bo tròn, 3 cạnh còn lại chạy sát viền màn hình)."""
    w, h = size
    mask = Image.new("L", size, 255)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([w - radius, 0, w - 1, radius - 1], fill=0)
    draw.pieslice([w - 2 * radius, 0, w, 2 * radius], 270, 360, fill=255)
    return mask


def _rounded_mask_corners(size, radius, corners=(False, True, False, False)):
    """Mask chữ nhật bo góc TUỲ CHỌN từng góc - corners=(trên-trái, trên-phải,
    dưới-phải, dưới-trái). Dùng cho khung ảnh trong các clip KOL khác nhau (mỗi clip
    có thể có khung ảnh ở vị trí/góc bo khác nhau, do người dùng tự khai báo)."""
    w, h = size
    tl, tr, br, bl = corners
    if radius <= 0 or not any(corners):
        return Image.new("L", size, 255)
    mask = Image.new("L", size, 255)
    draw = ImageDraw.Draw(mask)
    if tl:
        draw.rectangle([0, 0, radius - 1, radius - 1], fill=0)
        draw.pieslice([0, 0, 2 * radius, 2 * radius], 180, 270, fill=255)
    if tr:
        draw.rectangle([w - radius, 0, w - 1, radius - 1], fill=0)
        draw.pieslice([w - 2 * radius, 0, w, 2 * radius], 270, 360, fill=255)
    if br:
        draw.rectangle([w - radius, h - radius, w - 1, h - 1], fill=0)
        draw.pieslice([w - 2 * radius, h - 2 * radius, w, h], 0, 90, fill=255)
    if bl:
        draw.rectangle([0, h - radius, radius - 1, h - 1], fill=0)
        draw.pieslice([0, h - 2 * radius, 2 * radius, h], 90, 180, fill=255)
    return mask


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATIONFLAGS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def _fit_cover(img: Image.Image, size) -> Image.Image:
    tw, th = size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw, nh = int(math.ceil(sw * scale)), int(math.ceil(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _load_upload_reference(reference_path: Path) -> Image.Image:
    ref = Image.open(reference_path).convert("RGB")
    if ref.size != (W, H):
        ref = ref.resize((W, H), Image.LANCZOS)
    return ref


def detect_upload_box(ref: Image.Image, bright_threshold: int = 60) -> tuple[int, int, int, int]:
    """Tự dò vùng khung chứa ảnh trong 1 ảnh nền khung upload BẤT KỲ (không chỉ ảnh mặc
    định), dựa trên giả định: nền khung là màu ĐEN, vùng khung ảnh + chữ tiêu đề là 2
    mảng màu/sáng nổi bật tách biệt nhau bởi khoảng đen. Trả về (x, y, w, h) của mảng
    có DIỆN TÍCH LỚN NHẤT (thường là vùng khung ảnh, vì to hơn hẳn thanh tiêu đề)."""
    arr = np.asarray(ref, dtype=np.int32)
    bright = arr.sum(axis=2) > bright_threshold  # (H, W) bool, True = không phải nền đen

    row_has = bright.any(axis=1)
    bands = []
    in_band = False
    start = 0
    for y, v in enumerate(row_has):
        if v and not in_band:
            start, in_band = y, True
        elif not v and in_band:
            bands.append((start, y))
            in_band = False
    if in_band:
        bands.append((start, len(row_has)))
    if not bands:
        raise ValueError("Không tìm thấy vùng nào trong ảnh khung upload (ảnh toàn màu đen?).")

    best_box = None
    best_area = 0
    for y0, y1 in bands:
        col_has = bright[y0:y1].any(axis=0)
        xs = np.where(col_has)[0]
        if xs.size == 0:
            continue
        x0, x1 = int(xs[0]), int(xs[-1]) + 1
        area = (y1 - y0) * (x1 - x0)
        if area > best_area:
            best_area = area
            best_box = (x0, y0, x1 - x0, y1 - y0)

    if best_box is None:
        raise ValueError("Không dò được vùng khung ảnh phù hợp trong ảnh khung upload.")
    return best_box


def load_frame_geometry(reference_path: Path | None = None) -> dict:
    """Nạp 1 ảnh nền khung upload (mặc định hoặc do người dùng chọn) + tự dò vùng khung
    ảnh trong đó, trả về toàn bộ toạ độ cần dùng để dựng cảnh 'tải ảnh lên'."""
    path = Path(reference_path) if reference_path else UPLOAD_REFERENCE_PATH
    ref = _load_upload_reference(path)
    box = detect_upload_box(ref)
    box_x, box_y, box_w, box_h = box

    card_w, card_h = 420, 220
    card_x = (W - card_w) // 2
    card_y = box_y + box_h // 2 - card_h // 2
    ring_center = (card_x + card_w // 2, card_y + 78)

    return {
        "ref_image": ref,
        "box": box,
        "card": (card_x, card_y, card_w, card_h),
        "ring_center": ring_center,
    }


def build_upload_background(ref: Image.Image, box: tuple[int, int, int, int], out_path: Path) -> None:
    """Nền cho đoạn "tải ảnh lên": dùng đúng ảnh khung upload, chỉ xoá đen vùng khung
    (vùng đó sẽ được ghép đè riêng để có hiệu ứng trượt ảnh vào khung)."""
    x, y, w, h = box
    bg = ref.copy()
    bg.paste(Image.new("RGB", (w, h), (0, 0, 0)), (x, y))
    bg.save(out_path)


def build_frame_shape(ref: Image.Image, box: tuple[int, int, int, int], out_path: Path) -> None:
    """Cắt đúng vùng khung chọn ảnh từ ảnh nền khung upload (bo góc theo alpha)."""
    x, y, w, h = box
    crop = ref.crop((x, y, x + w, y + h))
    shape = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = _rounded_mask((w, h), BOX_RADIUS)
    shape.paste(crop, (0, 0), mask)
    shape.save(out_path)


def build_photo_masked(photo_path: Path, box_size: tuple[int, int], out_path: Path) -> None:
    w, h = box_size
    img = Image.open(photo_path).convert("RGB")
    img = _fit_cover(img, (w, h))
    rgba = img.convert("RGBA")
    mask = _rounded_mask((w, h), BOX_RADIUS)
    rgba.putalpha(mask)
    rgba.save(out_path)


RING_R = 40


def _gradient_fill_text(canvas: Image.Image, text: str, font: ImageFont.FreeTypeFont,
                         center_xy: tuple[int, int], start_color, end_color,
                         stroke_width: int = 3, stroke_fill=(0, 0, 0, 160)) -> None:
    """Vẽ chữ không nền, viền đen mờ để luôn rõ trên mọi ảnh, tô màu gradient ngang
    (trái -> phải) giống màu nền pill "UPLOAD YOUR PHOTO"."""
    draw = ImageDraw.Draw(canvas)
    # Viền đen mờ phía sau (chỉ viền, không tô) để chữ luôn đọc được trên ảnh sáng/tối.
    draw.text(center_xy, text, font=font, fill=(0, 0, 0, 0),
              stroke_width=stroke_width, stroke_fill=stroke_fill, anchor="mm")

    bbox = draw.textbbox(center_xy, text, font=font, anchor="mm")
    gx0, gy0, gx1, gy1 = bbox
    gw, gh = max(gx1 - gx0, 1), max(gy1 - gy0, 1)

    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).text(center_xy, text, font=font, fill=255, anchor="mm")

    grad_row = Image.new("RGB", (gw, 1))
    for x in range(gw):
        grad_row.putpixel((x, 0), _lerp_color(start_color, end_color, x / max(gw - 1, 1)))
    grad_block = grad_row.resize((gw, gh), Image.NEAREST).convert("RGBA")

    canvas.paste(grad_block, (gx0, gy0), mask.crop((gx0, gy0, gx1, gy1)))


def build_generating_card_base(card: tuple[int, int, int, int], out_path: Path,
                                text: str = GENERATING_TEXT) -> None:
    """Chữ "Generating..." KHÔNG có nền thẻ, chỉ chữ màu gradient (cam san hô -> tím,
    giống màu nền pill "UPLOAD YOUR PHOTO") có viền đen mờ để luôn rõ trên ảnh."""
    card_x, card_y, card_w, card_h = card
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    font = _font(30)
    text_cx = card_x + card_w // 2
    text_cy = card_y + 150
    _gradient_fill_text(canvas, text, font, (text_cx, text_cy),
                         HEADER_GRADIENT_START, HEADER_GRADIENT_END)
    canvas.save(out_path)


def build_generating_ring(out_path: Path) -> None:
    """Vòng xoay riêng (khung vuông trong suốt), sẽ được ffmpeg xoay theo thời gian.
    Thiết kế kiểu "comet tail": 1 vòng nền mờ nhạt + 1 vệt sáng gradient mờ dần ở đuôi,
    màu cam san hô -> tím giống màu nền pill "UPLOAD YOUR PHOTO"."""
    img = Image.new("RGBA", (RING_SIZE, RING_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = RING_SIZE // 2

    # Vòng nền mờ nhạt (track), luôn hiện đủ 360 độ.
    draw.arc([cx - RING_R, cy - RING_R, cx + RING_R, cy + RING_R],
              start=0, end=360, fill=(255, 255, 255, 55), width=9)

    # Vệt sáng gradient, quét ~280 độ, mờ dần về phía đuôi.
    start_color = HEADER_GRADIENT_START
    end_color = HEADER_GRADIENT_END
    sweep = 280
    segments = 70
    for i in range(segments):
        t0 = i / segments
        t1 = (i + 1) / segments
        a0 = sweep * t0
        a1 = sweep * t1
        alpha = int(255 * (1 - t0) ** 1.6)
        color = _lerp_color(start_color, end_color, t0)
        draw.arc([cx - RING_R, cy - RING_R, cx + RING_R, cy + RING_R],
                  start=-90 + a0, end=-90 + a1 + 1.2, fill=(*color, alpha), width=11)

    # Chấm sáng ở đầu vệt sáng (hiệu ứng "đầu sao chổi").
    head_angle = math.radians(-90)
    hx = cx + RING_R * math.cos(head_angle)
    hy = cy + RING_R * math.sin(head_angle)
    dot_r = 7
    draw.ellipse([hx - dot_r, hy - dot_r, hx + dot_r, hy + dot_r], fill=(*start_color, 255))

    img.save(out_path)


def _autocrop_border(img: Image.Image, tol: int = 18) -> Image.Image:
    """Tự cắt bỏ viền màu ĐỒNG NHẤT có sẵn trong ảnh gốc (thường là viền trắng mà nhiều
    tool AI tự chèn khi xuất ảnh theo khung cố định) - để tránh hiện viền trắng 2 bên
    khi ghép theo kiểu "hiện đủ ảnh + nền blur" (xem render_zoom_clip)."""
    rgb = img.convert("RGB")
    w, h = rgb.size

    def close(a, b) -> bool:
        return all(abs(a[i] - b[i]) <= tol for i in range(3))

    corners = [rgb.getpixel((1, 1)), rgb.getpixel((w - 2, 1)),
               rgb.getpixel((1, h - 2)), rgb.getpixel((w - 2, h - 2))]
    # Chỉ autocrop khi 4 góc đồng màu với nhau (đúng kiểu viền đệm, không phải ảnh chụp
    # bình thường có nội dung sát viền) - tránh crop nhầm ảnh thật.
    if not all(close(corners[0], c) for c in corners[1:]):
        return img

    bg = Image.new("RGB", rgb.size, corners[0])
    diff = ImageChops.difference(rgb, bg)
    diff = ImageChops.add(diff, diff, 2.0, -tol)
    bbox = diff.getbbox()
    if bbox and bbox != (0, 0, w, h):
        return img.crop(bbox)
    return img


def build_before_badge_overlay(before_path: Path, out_path: Path,
                                box: tuple[int, int, int, int] | None = None) -> None:
    """Dựng lớp overlay (RGBA, kích thước WxH, nền trong suốt) gồm: khung ảnh "before"
    thu nhỏ ở góc dưới-trái (bo góc, viền trắng, đổ bóng mềm) + chữ "Before" kiểu viết
    tay + mũi tên cong chỉ từ chữ vào khung - học theo mẫu quảng cáo before/after
    (kiểu "Hook 1": ảnh kết quả chạy full màn hình + khung before nhỏ, không tutorial)."""
    if box is None:
        margin = int(W * 0.055)
        box_w, box_h = int(W * 0.40), int(H * 0.285)
        box = (margin, H - margin - box_h, box_w, box_h)
    box_x, box_y, box_w, box_h = box
    border = max(6, box_w // 45)
    radius = max(16, box_w // 16)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Đổ bóng mềm phía sau khung before.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [box_x + 6, box_y + 10, box_x + box_w + 6, box_y + box_h + 10],
        radius=radius, fill=(0, 0, 0, 140),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow)

    # Viền trắng + ảnh before cover-fit bên trong (bo góc theo alpha).
    outer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    outer.paste(Image.new("RGBA", (box_w, box_h), (255, 255, 255, 255)),
                (0, 0), _rounded_mask((box_w, box_h), radius))

    inner_w, inner_h = box_w - border * 2, box_h - border * 2
    before_fit = _fit_cover(Image.open(before_path).convert("RGB"), (inner_w, inner_h)).convert("RGBA")
    before_fit.putalpha(_rounded_mask((inner_w, inner_h), max(8, radius - border)))
    outer.alpha_composite(before_fit, (border, border))

    canvas.alpha_composite(outer, (box_x, box_y))

    # Chữ "Before" kiểu viết tay, phía trên-phải khung.
    draw = ImageDraw.Draw(canvas)
    font = _script_font(int(box_w * 0.21))
    text_x = box_x + int(box_w * 0.62)
    text_y = box_y - int(box_h * 0.30)
    draw.text((text_x, text_y), "Before", font=font, fill=(255, 255, 255, 255),
              stroke_width=3, stroke_fill=(30, 20, 10, 220), anchor="lm")

    # Mũi tên cong từ chữ -> khung.
    p0 = (text_x + int(box_w * 0.06), text_y + int(box_h * 0.10))
    p1 = (text_x - int(box_w * 0.18), text_y + int(box_h * 0.42))
    p2 = (box_x + int(box_w * 0.40), box_y - int(box_h * 0.03))
    _draw_arrow_curve(draw, p0, p1, p2, color=(255, 255, 255, 255),
                       width=max(4, box_w // 55), head_size=max(14, box_w // 22))

    canvas.save(out_path)


def render_before_badge_clip(after_image: Path, before_image: Path, duration: float,
                              out_path: Path, tmp_dir: Path) -> None:
    """Hook 1: ảnh KẾT QUẢ chạy full màn hình (nền blur-fill, xem render_zoom_clip) +
    khung "Before" nhỏ góc dưới-trái (ảnh gốc) + mũi tên chỉ vào, KHÔNG có màn
    "tải ảnh lên"/tutorial."""
    bg_path = tmp_dir / f"{out_path.stem}_bg.mp4"
    badge_path = tmp_dir / f"{out_path.stem}_badge.png"
    render_zoom_clip(after_image, duration, bg_path)
    build_before_badge_overlay(before_image, badge_path)

    _run_ffmpeg([
        "-i", str(bg_path),
        "-loop", "1", "-i", str(badge_path),
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[vout]",
        "-map", "[vout]", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-frames:v", str(int(duration * FPS)), str(out_path),
    ])


def render_kol_pointer_clip(photo_path: Path, out_path: Path, tmp_dir: Path,
                             kol_video: Path | None = None,
                             box: tuple[int, int, int, int] | None = None,
                             corners: tuple[bool, bool, bool, bool] = (False, True, False, False),
                             radius: int = KOL_BOX_RADIUS) -> float:
    """Hook 3: dựng đoạn mở đầu dùng 1 clip KOL phản ứng/chỉ tay có sẵn, fit ảnh truyền
    vào (photo_path) đúng vào khung ảnh có sẵn trong clip đó, thay cho ảnh demo gốc.

    kol_video (tuỳ chọn): đường dẫn clip KOL - mặc định assets/kol_reaction_raw.mp4.
    box (tuỳ chọn): (x, y, w, h) vùng khung ảnh trong clip đó - mặc định KOL_BOX (clip mẫu).
    corners: (trên-trái, trên-phải, dưới-phải, dưới-trái) - góc nào được bo tròn -
    mặc định chỉ bo góc trên-phải (khớp clip mẫu). Mỗi clip KOL có thể có khung/góc bo
    khác nhau tuỳ người dùng khai báo khi thêm clip (xem core.load_kol_templates).
    Trả về thời lượng (giây) của clip KOL."""
    kol_video = Path(kol_video) if kol_video else DEFAULT_KOL_VIDEO
    if not kol_video.exists():
        raise FileNotFoundError(f"Không tìm thấy clip KOL: {kol_video}")

    box_x, box_y, box_w, box_h = box or KOL_BOX
    img = Image.open(photo_path).convert("RGB")
    fitted = _fit_cover(img, (box_w, box_h))
    mask = _rounded_mask_corners((box_w, box_h), radius, corners)
    overlay = Image.new("RGBA", (box_w, box_h))
    overlay.paste(fitted, (0, 0))
    overlay.putalpha(mask)

    overlay_path = tmp_dir / f"{out_path.stem}_photo.png"
    overlay.save(overlay_path)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(kol_video)],
        capture_output=True, text=True, creationflags=CREATIONFLAGS,
    )
    kol_dur = float(probe.stdout.strip() or SEG_DUR)

    _run_ffmpeg([
        "-i", str(kol_video),
        "-loop", "1", "-i", str(overlay_path),
        "-filter_complex", f"[0:v][1:v]overlay={box_x}:{box_y}:format=auto[vout]",
        "-map", "[vout]", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-frames:v", str(int(kol_dur * FPS)), "-an", str(out_path),
    ])
    return kol_dur


def render_zoom_clip(image_path: Path, duration: float, out_path: Path) -> None:
    """Hiện ảnh tĩnh (không zoom), chỉ dùng cho hiệu ứng chuyển cảnh (xfade) giữa các ảnh.

    Kiểu nền "letterbox blur" (học theo video mẫu 0411_chibi...): ảnh được hiện ĐẦY ĐỦ,
    không bị cắt mất nội dung - phần nền trống phía sau lấp bằng chính ảnh đó phóng to,
    làm mờ (gblur) + tối bớt, giống hiệu ứng lấp nền thường thấy trong các video AI demo.
    Trước đó tự cắt bỏ viền đệm có sẵn trong ảnh gốc (_autocrop_border) nếu có.
    """
    src_img = _autocrop_border(Image.open(image_path))
    src_path = out_path.parent / f"{out_path.stem}_src.png"
    src_img.save(src_path)

    frames = int(duration * FPS)
    vf = (
        f"split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"gblur=sigma=25,eq=brightness=-0.08[bgblur];"
        f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[fgfit];"
        f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2,fps={FPS}"
    )
    _run_ffmpeg([
        "-loop", "1", "-i", str(src_path),
        "-vf", vf, "-frames:v", str(frames),
        "-pix_fmt", "yuv420p", str(out_path),
    ])


def render_upload_clip(user_photo: Path, duration: float, out_path: Path,
                        tmp_dir: Path, upload_frame_path: Path | None = None) -> None:
    """upload_frame_path (tuỳ chọn): ảnh nền khung upload tuỳ chỉnh (thay cho ảnh mặc
    định assets/UPLOAD_YOUR_PHOTO_1.jpg) - vùng khung chứa ảnh sẽ được TỰ DÒ trong ảnh
    này (xem detect_upload_box)."""
    geo = load_frame_geometry(upload_frame_path)
    ref, box, card, ring_center = geo["ref_image"], geo["box"], geo["card"], geo["ring_center"]
    box_x, box_y, box_w, box_h = box
    RING_CENTER_X, RING_CENTER_Y = ring_center

    bg_path = tmp_dir / "upload_bg.png"
    frame_shape_path = tmp_dir / "frame_shape.png"
    photo_path = tmp_dir / "photo_masked.png"
    card_path = tmp_dir / "generating_card_base.png"
    ring_path = tmp_dir / "generating_ring.png"
    build_upload_background(ref, box, bg_path)
    build_frame_shape(ref, box, frame_shape_path)
    build_photo_masked(user_photo, (box_w, box_h), photo_path)
    build_generating_card_base(card, card_path)
    build_generating_ring(ring_path)

    y_start = H + 20
    slide_end = SLIDE_START + SLIDE_T
    card_start = slide_end  # ảnh vừa trượt vào khung xong là loading hiện luôn
    box_cx, box_cy = box_x + box_w / 2, box_y + box_h / 2

    # Ảnh user giữ nguyên kích thước (không phóng to/thu nhỏ), chỉ trượt vào đúng vị trí khung rồi đứng yên.
    overlay_y = (
        f"if(lt(t,{SLIDE_START}),{y_start},"
        f"if(lt(t,{slide_end}),{y_start}+({box_cy}-h/2-{y_start})*((t-{SLIDE_START})/{SLIDE_T}),"
        f"{box_cy}-h/2))"
    )
    rot_angle = f"2*PI*t*{RING_SPEED}"
    ring_canvas = int(math.ceil(RING_SIZE * math.sqrt(2)))
    if ring_canvas % 2:
        ring_canvas += 1

    # Phủ ngả xám + tối ngay từ lúc BẮT ĐẦU loading (card_start), tăng dần trong
    # DIM_RAMP giây rồi GIỮ NGUYÊN liên tục cho tới hết cảnh (tới khi cắt sang ảnh kết
    # quả tiếp theo) - học theo video mẫu 0411_chibi... Dùng "eq" (hỗ trợ eval=frame
    # đầy đủ, khác colorchannelmixer không hỗ trợ hàm min/max trong biểu thức "aa").
    sat_expr = (
        f"if(lt(t,{card_start}),1,1-{1 - SAT_MIN}*min(1,(t-{card_start})/{DIM_RAMP}))"
    )
    bright_expr = (
        f"if(lt(t,{card_start}),0,-{DIM_MAX}*min(1,(t-{card_start})/{DIM_RAMP}))"
    )

    filter_complex = (
        f"[0:v]format=yuva420p[bgv];"
        f"[1:v]format=yuva420p[fv];"
        f"[2:v]format=yuva420p[pv];"
        f"[3:v]format=yuva420p[cv];"
        f"[4:v]format=yuva420p,rotate=a='{rot_angle}':c=none:ow={ring_canvas}:oh={ring_canvas}[rv];"
        f"[bgv][fv]overlay=x='{box_cx}-w/2':y='{box_cy}-h/2':format=auto[tmp0];"
        f"[tmp0][pv]overlay=x='{box_cx}-w/2':y='{overlay_y}':format=auto[tmp1];"
        # Phủ ngả xám + tối CHỈ lên nền+ảnh (tmp1), áp dụng TRƯỚC khi ghép thẻ
        # "Generating..." + vòng xoay, để loading UI luôn rõ nét phía trên lớp phủ.
        f"[tmp1]eq=saturation='{sat_expr}':brightness='{bright_expr}':eval=frame[tmp1dim];"
        f"[tmp1dim][cv]overlay=x=0:y=0:enable='gte(t,{card_start})':format=auto[tmp2];"
        f"[tmp2][rv]overlay=x='{RING_CENTER_X}-w/2':y='{RING_CENTER_Y}-h/2':"
        f"enable='gte(t,{card_start})':format=auto[vout]"
    )
    _run_ffmpeg([
        "-loop", "1", "-t", str(duration), "-i", str(bg_path),
        "-loop", "1", "-t", str(duration), "-i", str(frame_shape_path),
        "-loop", "1", "-t", str(duration), "-i", str(photo_path),
        "-loop", "1", "-t", str(duration), "-i", str(card_path),
        "-loop", "1", "-t", str(duration), "-i", str(ring_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-frames:v", str(int(duration * FPS)), str(out_path),
    ])


def _xfade_concat(clip_paths: list[Path], durations: list[float], out_path: Path,
                   transitions: list[str] | None = None,
                   xfade_durations: list[float] | None = None) -> None:
    """transitions[i] = kiểu chuyển cảnh giữa clip i và clip i+1 (mặc định 'fade' nếu không set).
    xfade_durations[i] = thời lượng riêng cho chuyển cảnh đó (mặc định XFADE nếu không set)."""
    n = len(clip_paths)
    inputs = []
    for p in clip_paths:
        inputs += ["-i", str(p)]

    filter_parts = []
    prev_label = "0:v"
    cum = durations[0]
    for i in range(1, n):
        trans = transitions[i - 1] if transitions and i - 1 < len(transitions) else "fade"
        dur = xfade_durations[i - 1] if xfade_durations and i - 1 < len(xfade_durations) else XFADE
        offset = max(cum - dur, 0.0)
        out_label = f"v{i}" if i < n - 1 else "vout"
        filter_parts.append(
            f"[{prev_label}][{i}:v]xfade=transition={trans}:duration={dur}:offset={offset:.3f}[{out_label}]"
        )
        cum = cum + durations[i] - dur
        prev_label = out_label

    filter_complex = ";".join(filter_parts)
    _run_ffmpeg([
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "20", str(out_path),
    ])


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, creationflags=CREATIONFLAGS,
    )
    return float(proc.stdout.strip())


def overlay_flying_logo(video_in: Path, logo_path: Path, out_path: Path,
                         opacity: float = 0.15) -> None:
    """Ghép logo bay (có kênh alpha) chạy lặp lại xuyên suốt video_in. Video gốc không có audio.

    Lặp logo bằng `-stream_loop -1` trên INPUT (ffmpeg tự demux/decode lại từ đầu) thay
    vì filter `loop` (phải CACHE sẵn toàn bộ N frame đã decode ở dạng RGBA vào RAM - mỗi
    frame 1080x1920 yuva420p ~5MB, cache ~150 frame tốn ~750MB/process). Cache này nhân
    theo số video chạy song song (workers) nên dễ gây lỗi ffmpeg "Cannot allocate memory"
    khi chạy nhiều video cùng lúc. -stream_loop không cache gì, tốn RAM gần như hằng số."""
    main_dur = _probe_duration(video_in)

    filter_complex = (
        f"[1:v]scale={W}:{H},format=yuva420p,colorchannelmixer=aa={opacity},"
        f"trim=duration={main_dur:.3f},setpts=PTS-STARTPTS[logo];"
        f"[0:v][logo]overlay=0:0:format=auto[vout]"
    )
    _run_ffmpeg([
        "-i", str(video_in),
        "-stream_loop", "-1", "-i", str(logo_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "20", str(out_path),
    ])


def append_outtro(video_in: Path, outtro_path: Path, out_path: Path) -> None:
    """Nối thêm outtro (có audio riêng) vào cuối video_in (video_in không có audio)."""
    main_dur = _probe_duration(video_in)
    filter_complex = (
        f"[1:v]scale={W}:{H},setsar=1[outv];"
        f"[0:v]setsar=1[mainv];"
        f"[mainv][2:a][outv][1:a]concat=n=2:v=1:a=1[vout][aout]"
    )
    _run_ffmpeg([
        "-i", str(video_in),
        "-i", str(outtro_path),
        "-f", "lavfi", "-t", f"{main_dur:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-shortest", str(out_path),
    ])


def _has_audio_stream(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, creationflags=CREATIONFLAGS,
    )
    return bool(proc.stdout.strip())


def add_background_audio(video_in: Path, audio_path: Path, out_path: Path,
                          volume: float = 0.5) -> None:
    """Gán 1 file âm thanh (nhạc nền) vào video - tự lặp nếu ngắn hơn video, tự cắt nếu
    dài hơn. Nếu video đã có sẵn audio (vd. tiếng thoại/nhạc từ outtro) thì TRỘN
    (amix) với nhạc nền theo volume chỉ định, không ghi đè mất audio gốc."""
    main_dur = _probe_duration(video_in)
    has_audio = _has_audio_stream(video_in)

    if has_audio:
        filter_complex = (
            f"[1:a]volume={volume}[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        filter_complex = f"[1:a]volume={volume}[aout]"

    _run_ffmpeg([
        "-i", str(video_in),
        "-stream_loop", "-1", "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-t", f"{main_dur:.3f}",
        "-c:v", "copy", "-c:a", "aac", str(out_path),
    ])


def _resolve_pair_transitions(transition: str | list[str], n_pairs: int) -> list[str]:
    """transition: 1 tên transition (áp dụng chung cho mọi cặp ảnh liên tiếp) HOẶC 1
    list tên transition - transitions[i] = hiệu ứng giữa ảnh (i+1) và ảnh (i+2). Nếu
    list ngắn hơn số cặp cần, phần tử CUỐI được lặp lại cho các cặp còn thiếu."""
    if isinstance(transition, str):
        return [transition] * n_pairs
    lst = [t for t in transition if t]
    if not lst:
        return ["slideright"] * n_pairs
    if len(lst) < n_pairs:
        lst = lst + [lst[-1]] * (n_pairs - len(lst))
    return lst[:n_pairs]


def assemble_demo_video(
    before_images: list[Path],
    after_images: list[Path],
    user_photo: Path,
    out_path: Path,
    logo_path: Path | None = None,
    outtro_path: Path | None = None,
    audio_path: Path | None = None,
    audio_volume: float = 0.5,
    transition: str | list[str] = "slideright",
    upload_frame_path: Path | None = None,
    on_log=None,
) -> Path:
    """before_images: các ảnh hiện TRƯỚC đoạn "tải ảnh lên".
    after_images: các ảnh hiện SAU đoạn đó. user_photo: ảnh sẽ trượt vào khung.

    logo_path (tuỳ chọn): video logo bay có kênh alpha, lặp lại chạy xuyên suốt video.
    outtro_path (tuỳ chọn): video outtro nối thêm vào cuối.
    audio_path (tuỳ chọn): file nhạc nền gán vào video - tự lặp/cắt cho vừa thời lượng;
    nếu outtro có sẵn audio riêng thì nhạc nền được TRỘN chung (amix), không ghi đè.
    audio_volume: âm lượng nhạc nền (0-1) khi trộn.
    transition: 1 TÊN transition (ffmpeg xfade, áp dụng chung cho mọi cặp ảnh) HOẶC 1
    LIST tên transition - transition[i] = hiệu ứng riêng giữa ảnh kết quả thứ (i+1) và
    (i+2) (vd transition[0] = hiệu ứng giữa ảnh 1-2, transition[1] = giữa ảnh 2-3...).
    Các giá trị hợp lệ: "slideright", "slideleft", "fade"/"fadeblack", "circleopen",
    "wipeleft", "pixelize"... Riêng đoạn NGAY SAU khung upload luôn cắt thẳng (hard
    cut), không áp dụng transition này.
    upload_frame_path (tuỳ chọn): ảnh nền khung upload tuỳ chỉnh thay cho ảnh mặc định
    (xem render_upload_clip / detect_upload_box).
    """
    if not before_images and not after_images:
        raise ValueError("Cần ít nhất 1 ảnh (before hoặc after).")

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    out_path = Path(out_path)
    tmp_dir = out_path.parent / f"_tmp_{out_path.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        clip_paths = []
        durations = []

        for i, img in enumerate(before_images, start=1):
            log(f"Đang dựng ảnh trước khung upload #{i}...")
            c = tmp_dir / f"before_{i}.mp4"
            render_zoom_clip(img, SEG_DUR, c)
            clip_paths.append(c)
            durations.append(SEG_DUR)

        log("Đang dựng đoạn 'tải ảnh lên' (ảnh mẫu trượt vào khung)...")
        c_upload = tmp_dir / "upload.mp4"
        render_upload_clip(user_photo, UPLOAD_DUR, c_upload, tmp_dir, upload_frame_path=upload_frame_path)
        clip_paths.append(c_upload)
        durations.append(UPLOAD_DUR)

        for i, img in enumerate(after_images, start=1):
            log(f"Đang dựng ảnh sau khung upload #{i}...")
            c = tmp_dir / f"after_{i}.mp4"
            render_zoom_clip(img, SEG_DUR, c)
            clip_paths.append(c)
            durations.append(SEG_DUR)

        log("Đang ghép các đoạn với hiệu ứng chuyển cảnh...")
        n_joins = len(clip_paths) - 1
        transitions = ["slideright"] * n_joins
        xfade_durations = [XFADE] * n_joins

        # Chuyển cảnh GIỮA CÁC ẢNH KẾT QUẢ: mỗi cặp ảnh liên tiếp (1-2, 2-3,...) có thể
        # có hiệu ứng RIÊNG (transition là list) hoặc dùng chung 1 hiệu ứng (transition
        # là str). Áp dụng đồng nhất cho cả nhóm before (hiện trước khung upload, là 1
        # đoạn đầu của danh sách ảnh) và nhóm after (replay lại ĐỦ danh sách ảnh).
        before_pair_count = max(len(before_images) - 1, 0)
        after_pair_count = max(len(after_images) - 1, 0)
        pair_transitions = _resolve_pair_transitions(transition, max(before_pair_count, after_pair_count))
        for j in range(before_pair_count):
            transitions[j] = pair_transitions[j]

        upload_idx = len(before_images)
        after_join_base = upload_idx + 1
        for k in range(after_pair_count):
            transitions[after_join_base + k] = pair_transitions[k]

        # Lớp phủ ngả xám + tối đã làm NGAY TRONG cảnh upload (render_upload_clip, học
        # theo video mẫu 0411_chibi...), nên ở đây chỉ cần CẮT THẲNG (gần như không
        # xfade) sang cảnh tiếp theo, giống kiểu "hard cut" của video mẫu - không áp
        # dụng transition do người dùng chọn cho riêng đoạn này.
        if 0 <= upload_idx < n_joins:
            transitions[upload_idx] = "fade"
            xfade_durations[upload_idx] = 0.05

        base = tmp_dir / "base.mp4"
        _xfade_concat(clip_paths, durations, base, transitions=transitions, xfade_durations=xfade_durations)
        current = base

        if logo_path:
            log("Đang ghép logo bay chạy xuyên suốt video...")
            with_logo = tmp_dir / "with_logo.mp4"
            overlay_flying_logo(current, Path(logo_path), with_logo)
            current = with_logo

        if outtro_path:
            log("Đang nối outtro vào cuối video...")
            with_outtro = tmp_dir / "with_outtro.mp4"
            append_outtro(current, Path(outtro_path), with_outtro)
            current = with_outtro

        if audio_path:
            log("Đang gán nhạc nền vào video...")
            with_audio = tmp_dir / "with_audio.mp4"
            add_background_audio(current, Path(audio_path), with_audio, volume=audio_volume)
            current = with_audio

        shutil.copyfile(current, out_path)
        log(f"Xong: {out_path}")
        return out_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def assemble_before_badge_video(
    after_images: list[Path],
    before_image: Path,
    out_path: Path,
    logo_path: Path | None = None,
    outtro_path: Path | None = None,
    audio_path: Path | None = None,
    audio_volume: float = 0.5,
    transition: str | list[str] = "slideright",
    upload_frame_path: Path | None = None,
    replay_count: int = 2,
    kol: dict | None = None,
    before_images: list[Path] | None = None,
    replay_transition: str | list[str] | None = None,
    on_log=None,
) -> Path:
    """Hook 1: lần lượt hiện các ẢNH KẾT QUẢ (after_images) chạy full màn hình, mỗi ảnh
    luôn có khung "Before" nhỏ góc dưới-trái + mũi tên chỉ vào. Sau đó có đoạn "tải ảnh
    lên" (before_image trượt vào khung, giống assemble_demo_video), rồi REPLAY lại
    replay_count ảnh kết quả ĐẦU TIÊN (after_images[:replay_count]) chạy full màn hình
    bình thường - KHÔNG kèm khung Before nữa.

    before_images (tuỳ chọn): danh sách ảnh "Before" khớp 1-1 theo vị trí với
    after_images - mỗi ảnh kết quả sẽ hiện ĐÚNG ảnh gốc khớp tên của chính nó trong
    khung Before (xem build_auto_batch_tasks). Nếu không truyền, dùng chung
    before_image cho mọi ảnh (hành vi cũ, chỉ nên dùng khi chỉ có 1 ảnh gốc).
    before_image vẫn luôn là ảnh dùng để trượt vào khung ở đoạn "tải ảnh lên".

    kol (tuỳ chọn, Hook 3): dict {"video", "box"=[x,y,w,h], "corners"=[tl,tr,br,bl],
    "radius"} mô tả 1 clip KOL (xem core.load_kol_templates/render_kol_pointer_clip).
    Nếu có, chèn thêm 1 đoạn "KOL chỉ tay vào ảnh kết quả" ngay SAU nhóm badge và TRƯỚC
    đoạn tải ảnh lên (ảnh fit vào khung trong clip KOL là after_images[0]).

    transition: 1 tên transition dùng chung HOẶC 1 list transition[i] = hiệu ứng giữa
    ảnh (i+1) và (i+2), áp dụng cho nhóm badge (ảnh trước khung upload).
    replay_transition (tuỳ chọn): transition riêng cho nhóm REPLAY (ảnh sau khung
    upload) - cùng định dạng như `transition`. Nếu không truyền, dùng chung `transition`
    cho cả 2 nhóm (hành vi cũ).
    upload_frame_path (tuỳ chọn): ảnh nền khung upload tuỳ chỉnh (xem render_upload_clip).
    logo_path/outtro_path/audio_path: xem assemble_demo_video.
    """
    if not after_images:
        raise ValueError("Cần ít nhất 1 ảnh kết quả (after_images).")

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    out_path = Path(out_path)
    tmp_dir = out_path.parent / f"_tmp_{out_path.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        clip_paths = []
        durations = []
        for i, img in enumerate(after_images, start=1):
            b = before_images[i - 1] if before_images else before_image
            log(f"Đang dựng ảnh kết quả #{i} (kèm khung Before '{b.name}')...")
            c = tmp_dir / f"badge_{i}.mp4"
            render_before_badge_clip(img, b, SEG_DUR, c, tmp_dir)
            clip_paths.append(c)
            durations.append(SEG_DUR)

        if kol:
            log("Đang dựng đoạn KOL chỉ tay vào ảnh kết quả...")
            c_kol = tmp_dir / "kol.mp4"
            kol_dur = render_kol_pointer_clip(
                after_images[0], c_kol, tmp_dir,
                kol_video=kol.get("video"),
                box=tuple(kol["box"]) if kol.get("box") else None,
                corners=tuple(kol["corners"]) if kol.get("corners") else (False, True, False, False),
                radius=kol.get("radius", KOL_BOX_RADIUS),
            )
            clip_paths.append(c_kol)
            durations.append(kol_dur)

        log("Đang dựng đoạn 'tải ảnh lên' (ảnh gốc trượt vào khung)...")
        c_upload = tmp_dir / "upload.mp4"
        render_upload_clip(before_image, UPLOAD_DUR, c_upload, tmp_dir, upload_frame_path=upload_frame_path)
        clip_paths.append(c_upload)
        durations.append(UPLOAD_DUR)

        replay_images = after_images[:max(0, replay_count)]
        for i, img in enumerate(replay_images, start=1):
            log(f"Đang dựng ảnh kết quả (replay) #{i}...")
            c = tmp_dir / f"replay_{i}.mp4"
            render_zoom_clip(img, SEG_DUR, c)
            clip_paths.append(c)
            durations.append(SEG_DUR)

        if len(clip_paths) == 1:
            current = clip_paths[0]
        else:
            log("Đang ghép các đoạn với hiệu ứng chuyển cảnh...")
            n_joins = len(clip_paths) - 1
            transitions_list = ["slideright"] * n_joins
            xfade_durations = [XFADE] * n_joins

            badge_pair_count = max(len(after_images) - 1, 0)
            replay_pair_count = max(len(replay_images) - 1, 0)
            badge_transitions = _resolve_pair_transitions(transition, badge_pair_count)
            for j in range(badge_pair_count):
                transitions_list[j] = badge_transitions[j]

            upload_idx = len(after_images) + (1 if kol else 0)
            replay_join_base = upload_idx + 1
            replay_transitions = _resolve_pair_transitions(
                replay_transition if replay_transition is not None else transition, replay_pair_count
            )
            for k in range(replay_pair_count):
                transitions_list[replay_join_base + k] = replay_transitions[k]

            if 0 <= upload_idx < n_joins:
                transitions_list[upload_idx] = "fade"
                xfade_durations[upload_idx] = 0.05

            base = tmp_dir / "base.mp4"
            _xfade_concat(clip_paths, durations, base, transitions=transitions_list, xfade_durations=xfade_durations)
            current = base

        if logo_path:
            log("Đang ghép logo bay chạy xuyên suốt video...")
            with_logo = tmp_dir / "with_logo.mp4"
            overlay_flying_logo(current, Path(logo_path), with_logo)
            current = with_logo

        if outtro_path:
            log("Đang nối outtro vào cuối video...")
            with_outtro = tmp_dir / "with_outtro.mp4"
            append_outtro(current, Path(outtro_path), with_outtro)
            current = with_outtro

        if audio_path:
            log("Đang gán nhạc nền vào video...")
            with_audio = tmp_dir / "with_audio.mp4"
            add_background_audio(current, Path(audio_path), with_audio, volume=audio_volume)
            current = with_audio

        shutil.copyfile(current, out_path)
        log(f"Xong: {out_path}")
        return out_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _list_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def build_auto_batch_tasks(
    originals_dir: Path,
    results_dir: Path,
    out_dir: Path,
    on_log=None,
    rng: random.Random | None = None,
    result_count: int = 2,
) -> list[dict]:
    """Ghép cặp ảnh gốc <-> ảnh kết quả (khớp theo TÊN FILE, không tính đuôi file).

    Trả về list[dict] mỗi phần tử: {original, images, befores, out_path}.
    - images: danh sách result_count ảnh KẾT QUẢ (ảnh đầu tiên luôn là ảnh khớp tên với
      ảnh gốc, các ảnh còn lại chọn ngẫu nhiên trong số ảnh kết quả khác).
    - befores: danh sách ảnh GỐC tương ứng 1-1 với từng ảnh trong `images` (khớp theo
      tên file với từng ảnh kết quả đó, KHÔNG phải lúc nào cũng là `original`) - dùng
      để hiển thị đúng khung "Before" khớp tên cho mỗi ảnh kết quả.
    - Ảnh gốc (original) dùng để trượt vào khung upload.
    """
    rng = rng or random.Random()
    result_count = max(1, result_count)

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    originals = _list_images(Path(originals_dir))
    results_pool = _list_images(Path(results_dir))
    if not originals:
        raise ValueError(f"Không tìm thấy ảnh nào trong thư mục ảnh gốc: {originals_dir}")
    if not results_pool:
        raise ValueError(f"Không tìm thấy ảnh nào trong thư mục ảnh kết quả: {results_dir}")

    results_by_stem = {}
    for r in results_pool:
        results_by_stem.setdefault(r.stem, r)
    originals_by_stem = {}
    for o in originals:
        originals_by_stem.setdefault(o.stem, o)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = []
    for original in originals:
        matched_result = results_by_stem.get(original.stem)
        if matched_result is None:
            log(f"BỎ QUA '{original.name}': không tìm thấy ảnh kết quả cùng tên trong {results_dir}.")
            continue

        other_results = [r for r in results_pool if r != matched_result]
        need = result_count - 1
        if len(other_results) >= need:
            extra = rng.sample(other_results, need)
        elif other_results:
            log(f"CẢNH BÁO '{original.name}': chỉ có {len(other_results)} ảnh kết quả khác "
                f"(cần {need}), sẽ dùng lặp lại.")
            extra = [rng.choice(other_results) for _ in range(need)]
        else:
            log(f"CẢNH BÁO '{original.name}': không có ảnh kết quả nào khác, dùng tạm lại ảnh khớp tên.")
            extra = [matched_result] * need

        images = [matched_result] + extra
        befores = [originals_by_stem.get(img.stem, original) for img in images]
        tasks.append({
            "original": original,
            "images": images,
            "befores": befores,
            "out_path": out_dir / f"{original.stem}.mp4",
        })
    return tasks


def auto_batch_from_folders(
    originals_dir: Path,
    results_dir: Path,
    out_dir: Path,
    logo_path: Path | None = None,
    outtro_path: Path | None = None,
    audio_path: Path | None = None,
    audio_volume: float = 0.5,
    result_count: int = 2,
    before_count: int | None = None,
    transition: str | list[str] = "slideright",
    upload_frame_path: Path | None = None,
    on_log=None,
    rng: random.Random | None = None,
    max_workers: int = 1,
) -> list[Path]:
    """Với mỗi ảnh gốc (ảnh user tải lên) trong originals_dir, tìm đúng ảnh kết quả
    tương ứng trong results_dir, rồi tự tạo 1 video (xem build_auto_batch_tasks).
    Tên file video đầu ra đặt theo đúng tên ảnh gốc.

    audio_path (tuỳ chọn): nhạc nền gán chung cho mọi video (xem assemble_demo_video).
    result_count: số ảnh kết quả dùng cho mỗi video (mặc định 2, như trước).
    before_count: số ảnh kết quả hiện TRƯỚC khung upload (mặc định = result_count,
      tức hiện hết trước rồi mới tới khung upload, giống hành vi cũ). Nếu nhỏ hơn
      result_count, khung upload sẽ xuất hiện SỚM hơn (sau ít ảnh hơn) - "vị trí"
      khung upload trong video coi như tuỳ chỉnh được qua tham số này. Đoạn sau khung
      upload luôn hiện lại ĐỦ result_count ảnh (replay toàn bộ).
    transition: 1 tên hiệu ứng DÙNG CHUNG cho mọi cặp ảnh, HOẶC 1 list tên hiệu ứng -
      transition[i] = hiệu ứng riêng giữa ảnh kết quả (i+1) và (i+2) (vd "slideright",
      "slideleft", "fade"/"fadeblack", "circleopen", "wipeleft", "pixelize"...,
      xem danh sách transition hợp lệ của ffmpeg xfade). Đoạn chuyển ngay sau khung
      upload luôn CẮT THẲNG (hard cut), không áp dụng transition này.

    max_workers > 1: chạy song song nhiều video cùng lúc (mỗi video vẫn dùng ffmpeg
    tuần tự bên trong, nhưng nhiều video chạy đồng thời qua ThreadPoolExecutor).
    """
    result_count = max(1, result_count)
    before_count = result_count if before_count is None else max(1, min(before_count, result_count))

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    tasks = build_auto_batch_tasks(
        originals_dir, results_dir, out_dir, on_log=on_log, rng=rng, result_count=result_count,
    )

    def run_one(task: dict) -> Path:
        name = task["original"].stem
        images = task["images"]

        def task_log(msg: str) -> None:
            log(f"[{name}] {msg}")

        task_log(
            "===== Đang tạo video (ảnh kết quả: " +
            ", ".join(f"'{img.name}'" for img in images) + ") ====="
        )
        return assemble_demo_video(
            before_images=images[:before_count],
            after_images=images,
            user_photo=task["original"],
            out_path=task["out_path"],
            logo_path=logo_path,
            outtro_path=outtro_path,
            audio_path=audio_path,
            audio_volume=audio_volume,
            transition=transition,
            upload_frame_path=upload_frame_path,
            on_log=task_log,
        )

    outputs = []
    if max_workers <= 1 or len(tasks) <= 1:
        for task in tasks:
            outputs.append(run_one(task))
        return outputs

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run_one, task): task for task in tasks}
        for fut in as_completed(futures):
            task = futures[fut]
            try:
                outputs.append(fut.result())
            except Exception as exc:
                log(f"[{task['original'].stem}] LỖI: {exc}")
    return outputs


def auto_before_badge_from_folders(
    originals_dir: Path,
    results_dir: Path,
    out_dir: Path,
    logo_path: Path | None = None,
    outtro_path: Path | None = None,
    audio_path: Path | None = None,
    audio_volume: float = 0.5,
    result_count: int = 2,
    transition: str | list[str] = "slideright",
    upload_frame_path: Path | None = None,
    replay_count: int = 2,
    kol_templates: list[dict] | None = None,
    replay_transition: str | list[str] | None = None,
    on_log=None,
    rng: random.Random | None = None,
    max_workers: int = 1,
) -> list[Path]:
    """Hook 1/3 - bản hàng loạt: với mỗi ảnh gốc (before) trong originals_dir, ghép với
    result_count ảnh kết quả (after) khớp/ngẫu nhiên trong results_dir (xem
    build_auto_batch_tasks), rồi tạo 1 video kiểu "Hook 1" (xem assemble_before_badge_video):
    các ảnh after chạy lần lượt full màn hình kèm khung Before, sau đó có đoạn tải ảnh lên
    rồi replay lại replay_count ảnh kết quả đầu (không khung Before).

    kol_templates (tuỳ chọn, bật Hook 3): danh sách clip KOL đã khai báo (xem
    core.load_kol_templates). Nếu có, MỖI VIDEO trong batch sẽ được chèn thêm 1 đoạn
    KOL chỉ tay vào ảnh, chọn NGẪU NHIÊN 1 clip trong danh sách này cho mỗi video -
    nhiều video khác nhau sẽ dùng nhân vật KOL khác nhau."""
    rng = rng or random.Random()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    tasks = build_auto_batch_tasks(
        originals_dir, results_dir, out_dir, on_log=on_log, rng=rng, result_count=result_count,
    )

    def run_one(task: dict) -> Path:
        name = task["original"].stem
        images = task["images"]
        kol = rng.choice(kol_templates) if kol_templates else None

        def task_log(msg: str) -> None:
            log(f"[{name}] {msg}")

        task_log(
            "===== Đang tạo video Hook 1 (ảnh kết quả: " +
            ", ".join(f"'{img.name}'" for img in images) + ")" +
            (f" + KOL '{kol.get('name', '?')}'" if kol else "") + " ====="
        )
        return assemble_before_badge_video(
            after_images=images,
            before_image=task["original"],
            before_images=task["befores"],
            out_path=task["out_path"],
            logo_path=logo_path,
            outtro_path=outtro_path,
            audio_path=audio_path,
            audio_volume=audio_volume,
            transition=transition,
            upload_frame_path=upload_frame_path,
            replay_count=replay_count,
            kol=kol,
            replay_transition=replay_transition,
            on_log=task_log,
        )

    outputs = []
    if max_workers <= 1 or len(tasks) <= 1:
        for task in tasks:
            outputs.append(run_one(task))
        return outputs

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run_one, task): task for task in tasks}
        for fut in as_completed(futures):
            task = futures[fut]
            try:
                outputs.append(fut.result())
            except Exception as exc:
                log(f"[{task['original'].stem}] LỖI: {exc}")
    return outputs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Tạo video demo trend + ảnh mẫu.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_manual = sub.add_parser("manual", help="Tự chỉ định ảnh before/after + ảnh mẫu.")
    p_manual.add_argument("--before", nargs="+", required=True, help="Các ảnh hiện TRƯỚC khung upload.")
    p_manual.add_argument("--after", nargs="+", required=True, help="Các ảnh hiện SAU khung upload.")
    p_manual.add_argument("--photo", required=True, help="Ảnh mẫu user sẽ trượt vào khung.")
    p_manual.add_argument("--out", required=True, help="File video đầu ra (.mp4).")
    p_manual.add_argument("--logo", default=None)
    p_manual.add_argument("--outtro", default=None)
    p_manual.add_argument("--audio", default=None, help="File nhạc nền (tuỳ chọn).")
    p_manual.add_argument("--audio-volume", type=float, default=0.5)
    p_manual.add_argument("--transition", default="slideright", help="Hiệu ứng chuyển cảnh giữa ảnh.")
    p_manual.add_argument("--upload-frame", default=None, help="Ảnh nền khung upload tuỳ chỉnh (tuỳ chọn).")

    p_auto = sub.add_parser("auto", help="Tự động tạo video hàng loạt từ 2 thư mục.")
    p_auto.add_argument("--originals", required=True, help="Thư mục ảnh gốc (user tải lên).")
    p_auto.add_argument("--results", required=True,
                         help="Thư mục ảnh kết quả, tên file khớp với ảnh gốc tương ứng.")
    p_auto.add_argument("--out-dir", required=True, help="Thư mục lưu các video đầu ra.")
    p_auto.add_argument("--logo", default=None)
    p_auto.add_argument("--outtro", default=None)
    p_auto.add_argument("--audio", default=None, help="File nhạc nền gán chung cho mọi video (tuỳ chọn).")
    p_auto.add_argument("--audio-volume", type=float, default=0.5)
    p_auto.add_argument("--result-count", type=int, default=2, help="Số ảnh kết quả mỗi video.")
    p_auto.add_argument("--before-count", type=int, default=None,
                         help="Số ảnh hiện trước khung upload (mặc định = result-count).")
    p_auto.add_argument("--transition", default="slideright", help="Hiệu ứng chuyển cảnh giữa ảnh.")
    p_auto.add_argument("--upload-frame", default=None, help="Ảnh nền khung upload tuỳ chỉnh (tuỳ chọn).")
    p_auto.add_argument("--workers", type=int, default=1, help="Số video chạy song song.")

    p_hook1 = sub.add_parser("hook1", help="Tạo video kiểu Hook 1 (after full màn + khung Before).")
    p_hook1.add_argument("--originals", required=True, help="Thư mục ảnh gốc (before).")
    p_hook1.add_argument("--results", required=True,
                          help="Thư mục ảnh kết quả (after), tên file khớp với ảnh gốc tương ứng.")
    p_hook1.add_argument("--out-dir", required=True, help="Thư mục lưu các video đầu ra.")
    p_hook1.add_argument("--logo", default=None)
    p_hook1.add_argument("--outtro", default=None)
    p_hook1.add_argument("--audio", default=None)
    p_hook1.add_argument("--audio-volume", type=float, default=0.5)
    p_hook1.add_argument("--result-count", type=int, default=2, help="Số ảnh kết quả mỗi video.")
    p_hook1.add_argument("--transition", default="slideright")
    p_hook1.add_argument("--upload-frame", default=None, help="Ảnh nền khung upload tuỳ chỉnh (tuỳ chọn).")
    p_hook1.add_argument("--replay-count", type=int, default=2,
                          help="Số ảnh kết quả đầu được replay lại sau đoạn tải ảnh lên.")
    p_hook1.add_argument("--workers", type=int, default=1)

    args = parser.parse_args()

    if args.cmd == "manual":
        assemble_demo_video(
            [Path(p) for p in args.before], [Path(p) for p in args.after],
            Path(args.photo), Path(args.out),
            logo_path=Path(args.logo) if args.logo else None,
            outtro_path=Path(args.outtro) if args.outtro else None,
            audio_path=Path(args.audio) if args.audio else None,
            audio_volume=args.audio_volume,
            transition=args.transition,
            upload_frame_path=Path(args.upload_frame) if args.upload_frame else None,
            on_log=lambda m: print(m, flush=True),
        )
    elif args.cmd == "auto":
        auto_batch_from_folders(
            Path(args.originals), Path(args.results), Path(args.out_dir),
            logo_path=Path(args.logo) if args.logo else None,
            outtro_path=Path(args.outtro) if args.outtro else None,
            audio_path=Path(args.audio) if args.audio else None,
            audio_volume=args.audio_volume,
            result_count=args.result_count,
            before_count=args.before_count,
            transition=args.transition,
            upload_frame_path=Path(args.upload_frame) if args.upload_frame else None,
            max_workers=args.workers,
            on_log=lambda m: print(m, flush=True),
        )
    else:
        auto_before_badge_from_folders(
            Path(args.originals), Path(args.results), Path(args.out_dir),
            logo_path=Path(args.logo) if args.logo else None,
            outtro_path=Path(args.outtro) if args.outtro else None,
            audio_path=Path(args.audio) if args.audio else None,
            audio_volume=args.audio_volume,
            result_count=args.result_count,
            transition=args.transition,
            upload_frame_path=Path(args.upload_frame) if args.upload_frame else None,
            replay_count=args.replay_count,
            max_workers=args.workers,
            on_log=lambda m: print(m, flush=True),
        )
