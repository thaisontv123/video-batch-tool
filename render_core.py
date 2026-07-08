"""
Lõi xử lý render (render core) cho Auto Batch Video Render Tool.

Toàn bộ logic ffmpeg ở đây được giữ NGUYÊN theo phiên bản CLI đã test thực tế
(xem spec mục 3 & 4). Đặc biệt lưu ý:
  - Scale lại sau bước camera-shake crop (nếu thiếu sẽ lỗi "Failed to configure output pad").
  - Resolution nhất quán xuyên suốt.
  - -pix_fmt yuv420p, -r {fps} tường minh, -shortest cho snow -stream_loop -1.
  - Escape đường dẫn subtitle (\\ -> /, escape ':' và "'").

Module này KHÔNG phụ thuộc GUI để có thể test/độc lập.
"""

import os
import re
import glob
import shutil
import tempfile
import threading
import subprocess

# ---------------------------------------------------------------------------
# Nhận diện file theo phần mở rộng (không phụ thuộc tên file cụ thể)
# ---------------------------------------------------------------------------
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac")
SUB_EXT = (".srt",)

# Cờ ẩn cửa sổ console khi gọi ffmpeg trên Windows (tránh nháy cmd đen)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Bảng resolution (nhãn UI -> chuỗi ffmpeg "W:H")
RESOLUTIONS = {
    "480p": "854:480",
    "720p": "1280:720",
    "1080p": "1920:1080",
    "4K": "3840:2160",
}

# Các bộ mã hóa GPU sẽ được dò tự động (key ffmpeg -> nhãn UI)
GPU_ENCODERS = [
    ("h264_nvenc", "GPU NVIDIA (NVENC)"),
    ("h264_amf", "GPU AMD (AMF)"),
    ("h264_qsv", "GPU Intel (QSV)"),
]
CPU_ENCODER = {"key": "libx264", "label": "CPU (x264)"}


def default_config():
    """CONFIG mặc định (đã tách khỏi code cứng, UI sẽ ghi đè từng trường)."""
    return {
        "root_dir": "",
        "output_name": "output.mp4",
        "resolution": "1920:1080",
        "fps": 24,
        "crf": 20,
        "preset": "medium",
        "encoder": "libx264",   # libx264 (CPU) | h264_nvenc | h264_amf | h264_qsv
        "filter_threads": max(2, (os.cpu_count() or 4) // 3),  # giới hạn CPU/video
        "effects": {
            "snow_overlay": True,
            "smooth_shake": True,     # rung máy nhẹ, mượt (thay cho shake + zoom cũ)
            "sound_wave": False,      # overlay sóng âm (video sáng trên nền đen)
            "glow_subtitle": False,
        },
        "snow_video_path": "",   # đường dẫn tuyệt đối tới snow.mp4 (chọn qua UI)
        "snow_opacity": 0.6,
        "wave_video_path": "",   # đường dẫn tuyệt đối tới video sóng âm (chọn qua UI)
        "wave_opacity": 0.9,
        "smooth_shake_strength": 20,   # biên độ lắc ngang (px), càng lớn càng trôi rộng
        "shake_supersample": 2,        # độ mượt lắc: 2=Thường(nhanh), 3=Cao(mượt hơn)
        "subtitle": {
            "font_name": "Arial",
            "font_size": 22,
            "font_color": "&H00FFFFFF",     # định dạng ASS &HAABBGGRR
            "outline_color": "&H00000000",
            "outline_width": 2,
            "bold": False,
            "alignment": 2,       # 2 = căn giữa-đáy (ASS numpad)
            "margin_v": 10,       # lề dọc ASS (~PlayResY 288); 10 ≈ sát đáy, nhấc nhẹ
        },
        "max_workers": max(1, (os.cpu_count() or 2) - 1),
        "log_file": "render_log.txt",
    }


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------
def find_file(folder, extensions):
    """Tìm file đầu tiên trong folder khớp 1 trong các phần mở rộng."""
    for ext in extensions:
        matches = glob.glob(os.path.join(folder, f"*{ext}"))
        if matches:
            return matches[0]
    return None


def escape_ffmpeg_path(path):
    """Escape đường dẫn subtitle để nhét an toàn vào filter subtitles='...'."""
    path = os.path.abspath(path).replace("\\", "/")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


def hex_to_ass(hex_color):
    """Convert màu hex '#RRGGBB' (hoặc '#AARRGGBB') sang định dạng ASS '&HAABBGGRR'.

    ASS đảo thứ tự byte thành BGR, alpha đứng đầu. Alpha trong ASS: 00 = đục hoàn
    toàn, FF = trong suốt (ngược với hình dung thông thường), mặc định 00.
    """
    if hex_color is None:
        return "&H00FFFFFF"
    s = hex_color.strip().lstrip("#")
    if len(s) == 6:
        aa = "00"
        rr, gg, bb = s[0:2], s[2:4], s[4:6]
    elif len(s) == 8:
        aa, rr, gg, bb = s[0:2], s[2:4], s[4:6], s[6:8]
    else:
        return "&H00FFFFFF"
    return f"&H{aa}{bb}{gg}{rr}".upper()


def ass_to_hex(ass_color):
    """Convert ngược '&HAABBGGRR' -> '#RRGGBB' (bỏ alpha) để hiển thị lên color picker."""
    m = re.match(r"&H([0-9A-Fa-f]{8})", ass_color or "")
    if not m:
        return "#FFFFFF"
    v = m.group(1).upper()
    bb, gg, rr = v[2:4], v[4:6], v[6:8]
    return f"#{rr}{gg}{bb}"


def check_ffmpeg():
    """Kiểm tra ffmpeg có trên PATH không. Trả về (ok: bool, version_or_msg: str)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False, "Không tìm thấy ffmpeg trên hệ thống (PATH)."
    try:
        r = subprocess.run(
            [exe, "-version"], capture_output=True, text=True,
            creationflags=_NO_WINDOW,
        )
        first = (r.stdout or "").splitlines()[0] if r.stdout else "ffmpeg"
        return True, first
    except Exception as e:  # noqa: BLE001
        return False, f"Lỗi khi chạy ffmpeg: {e}"


def _ffmpeg_exe():
    return shutil.which("ffmpeg") or "ffmpeg"


def test_encoder(key):
    """Encode thử 1 clip nhỏ để xác nhận encoder thực sự chạy được trên máy này.

    Nhiều máy có ffmpeg liệt kê h264_nvenc nhưng driver cũ/không có GPU -> fail.
    Chỉ những encoder pass test mới được đưa lên UI.
    """
    cmd = [
        _ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
        "-r", "25", "-frames:v", "25",
        "-c:v", key, "-pix_fmt", "yuv420p",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_NO_WINDOW, timeout=25)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def detect_encoders():
    """Danh sách encoder khả dụng: luôn có CPU, cộng các GPU pass test.

    Trả về list dict [{"key","label"}], phần tử đầu là mặc định nên chọn
    (ưu tiên GPU nếu có).
    """
    exe = _ffmpeg_exe()
    try:
        listing = subprocess.run([exe, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True,
                                 creationflags=_NO_WINDOW, timeout=25).stdout or ""
    except Exception:  # noqa: BLE001
        listing = ""
    gpus = []
    for key, label in GPU_ENCODERS:
        if key in listing and test_encoder(key):
            gpus.append({"key": key, "label": label})
    # GPU lên đầu (mặc định), CPU luôn có mặt ở cuối
    return gpus + [dict(CPU_ENCODER)]


def video_encode_args(encoder, cfg):
    """Sinh tham số encode video theo encoder đã chọn. CRF/CQ dùng chung thang 18–28.

    Filter (scale/zoompan/crop/blend/subtitles) vẫn chạy CPU — chỉ bước ENCODE
    được đẩy sang GPU, nên toàn bộ logic hiệu ứng đã test giữ nguyên.
    """
    q = str(cfg["crf"])
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", q, "-b:v", "0", "-pix_fmt", "yuv420p"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "balanced",
                "-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q, "-pix_fmt", "yuv420p"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium",
                "-global_quality", q, "-pix_fmt", "yuv420p"]
    # mặc định CPU libx264
    return ["-c:v", "libx264", "-preset", cfg["preset"], "-crf", q,
            "-pix_fmt", "yuv420p"]


def probe_duration(path):
    """Lấy độ dài (giây) của file media bằng ffprobe — dùng để tính % tiến độ."""
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=20)
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return None


def find_projects(root):
    """Danh sách thư mục con (mỗi cái = 1 project), sắp xếp theo tên."""
    if not root or not os.path.isdir(root):
        return []
    return [
        os.path.join(root, f)
        for f in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, f))
    ]


def overlay_indices(cfg):
    """Chỉ số input ffmpeg của các lớp overlay, theo đúng thứ tự prepare_command thêm.

    Input cố định: 0 = ảnh, 1 = audio. Sau đó lần lượt: snow (nếu bật), sóng âm (nếu bật).
    Trả về (snow_idx, wave_idx); None nếu lớp đó tắt.
    """
    idx = 2
    snow_idx = wave_idx = None
    eff = cfg.get("effects", {})
    if eff.get("snow_overlay"):
        snow_idx = idx
        idx += 1
    if eff.get("sound_wave"):
        wave_idx = idx
        idx += 1
    return snow_idx, wave_idx


def project_status(folder, need_snow, snow_path, need_wave=False, wave_path=None):
    """Trả về danh sách file còn thiếu của 1 project (rỗng = đủ điều kiện render)."""
    img = find_file(folder, IMG_EXT)
    audio = find_file(folder, AUDIO_EXT)
    sub = find_file(folder, SUB_EXT)
    missing = [name for name, f in
               [("ảnh", img), ("audio", audio), ("phụ đề .srt", sub)] if f is None]
    if need_snow and not (snow_path and os.path.exists(snow_path)):
        missing.append("snow.mp4")
    if need_wave and not (wave_path and os.path.exists(wave_path)):
        missing.append("file sóng âm")
    return missing


# ---------------------------------------------------------------------------
# Xây dựng filter_complex (GIỮ NGUYÊN logic đã test — spec mục 4)
# ---------------------------------------------------------------------------
def build_filter_complex(cfg, sub_path):
    res = cfg["resolution"]
    w, h = res.split(":")
    effects = cfg["effects"]

    steps = [f"[0:v]scale={res}:force_original_aspect_ratio=increase,crop={res}"]

    if effects.get("smooth_shake"):
        # LẮC mượt theo quỹ đạo PARABOL (vòng cung), không phải trượt thẳng ngang:
        #   - Ngang (x): 1 sóng sin CHẬM chu kỳ 8s -> trôi trái<->phải như con lắc.
        #   - Dọc (y): dao động ở tần số GẤP ĐÔI (chu kỳ 4s) khớp với x -> vì y ~ x²,
        #     khi ảnh đi trái->giữa->phải nó vẽ 1 CUNG CONG (nhấc lên ở giữa) như parabol.
        # velocity=0 ở 2 đầu nên đổi chiều rất êm; không sóng tần số cao -> không rung/giật.
        # Biên độ ngang 0.9*s, dọc 0.35*s (đều < s) -> cửa sổ crop luôn trong khung.
        s = int(cfg.get("smooth_shake_strength", 20))
        xexpr = f"{s}+{s}*0.90*sin(2*PI*t/8.0)"
        yexpr = f"{s}-{s}*0.35*cos(2*PI*t/4.0)"
        # SUPERSAMPLING chống GIẬT: crop chỉ dịch được nguyên pixel, nên lắc chậm sẽ
        # nhảy 0,1,0,1 px (giật). Phóng nền lên Fx (bilinear) rồi crop-pan trong không
        # gian Fx, sau đó thu nhỏ về res (bicubic) -> vị trí lẻ pixel được nội suy mượt.
        # F càng lớn càng mượt nhưng càng chậm (F=2 ~2.5x, F=3 ~6x thời gian lọc).
        # Người dùng chọn qua UI "Độ mượt": Thường=2, Cao=3.
        F = max(1, int(cfg.get("shake_supersample", 2)))
        steps.append(f"scale=iw*{F}:ih*{F}:flags=bilinear")
        steps.append(
            f"crop=in_w-{s*2*F}:in_h-{s*2*F}:x='({xexpr})*{F}':y='({yexpr})*{F}'")
        steps.append(f"scale={res}:flags=bicubic")  # thu nhỏ + scale về đúng resolution

    bg_chain = ",".join(steps) + "[bg]"
    filter_parts = [bg_chain]
    last_label = "[bg]"

    # Các lớp overlay dạng "sáng trên nền đen" (tuyết, sóng âm) đều chồng bằng blend
    # 'screen'. QUAN TRỌNG: phải chạy trong RGB (gbrp) — nếu để YUV, phép screen tác
    # động lên 2 kênh chroma Cb/Cr (trung tính 128) đẩy lên ~192 -> ám HỒNG/tím toàn
    # khung. Convert nền + overlay sang gbrp thì nền đen mới thực sự "biến mất".
    snow_idx, wave_idx = overlay_indices(cfg)
    overlays = []
    if snow_idx is not None:
        overlays.append((snow_idx, cfg.get("snow_opacity", 0.6)))
    if wave_idx is not None:
        overlays.append((wave_idx, cfg.get("wave_opacity", 0.9)))

    if overlays:
        filter_parts.append(f"{last_label}format=gbrp[bgrgb]")
        last_label = "[bgrgb]"
        for i, (idx, opacity) in enumerate(overlays):
            filter_parts.append(f"[{idx}:v]scale={res},format=gbrp[ov{i}s]")
            out = f"[ov{i}]"
            filter_parts.append(
                f"{last_label}[ov{i}s]blend=all_mode=screen:"
                f"all_opacity={opacity}{out}")
            last_label = out

    sub = cfg["subtitle"]
    style = (
        f"FontName={sub['font_name']},FontSize={sub['font_size']},"
        f"PrimaryColour={sub['font_color']},OutlineColour={sub['outline_color']},"
        f"Outline={sub['outline_width']},Bold={1 if sub['bold'] else 0},"
        f"Alignment={sub.get('alignment', 2)},MarginV={sub.get('margin_v', 20)}"
    )
    if effects["glow_subtitle"]:
        style = style.replace(f"Outline={sub['outline_width']}", "Outline=4")

    filter_parts.append(f"{last_label}subtitles='{sub_path}':force_style='{style}'[vout]")
    return ";".join(filter_parts)


# ---------------------------------------------------------------------------
# Chuẩn bị lệnh render cho 1 project (thuần, không thực thi — dễ test)
# ---------------------------------------------------------------------------
def prepare_command(folder, cfg):
    """
    Trả về dict:
      { ok: bool, cmd: list|None, output: str|None, missing: str|None,
        duration: float|None }
    - Nếu thiếu file: ok=False, missing="thiếu ..." (KHÔNG raise, để batch không dừng).
    - duration = độ dài audio (giây) để tính % tiến độ khi render.
    """
    img = find_file(folder, IMG_EXT)
    audio = find_file(folder, AUDIO_EXT)
    sub = find_file(folder, SUB_EXT)
    snow = cfg.get("snow_video_path") or ""
    wave = cfg.get("wave_video_path") or ""
    out = os.path.join(folder, cfg["output_name"])

    missing = [name for name, f in
               [("ảnh", img), ("audio", audio), ("phụ đề .srt", sub)] if f is None]
    if cfg["effects"]["snow_overlay"] and not (snow and os.path.exists(snow)):
        missing.append("snow.mp4")
    if cfg["effects"].get("sound_wave") and not (wave and os.path.exists(wave)):
        missing.append("file sóng âm")
    if missing:
        return {"ok": False, "cmd": None, "output": out,
                "missing": f"Thiếu {', '.join(missing)}", "duration": None,
                "temp_sub": None}

    # Copy .srt sang FILE TẠM có đường dẫn ASCII an toàn rồi mới đưa vào filter
    # 'subtitles='. Lý do: filter này bao đường dẫn bằng dấu nháy đơn ', nên nếu đường
    # dẫn gốc chứa dấu ' (vd tên "'I'll Be...'") sẽ vỡ lệnh ("No option name...").
    # Dùng file tạm né hoàn toàn mọi ký tự đặc biệt/nháy/tiếng Việt trong path.
    temp_sub = None
    try:
        fd, temp_sub = tempfile.mkstemp(suffix=".srt", prefix="vbt_")
        os.close(fd)
        shutil.copyfile(sub, temp_sub)
        sub_for_filter = temp_sub
    except Exception:  # noqa: BLE001
        sub_for_filter = sub  # dự phòng: dùng path gốc nếu copy lỗi

    sub_escaped = escape_ffmpeg_path(sub_for_filter)
    filter_complex = build_filter_complex(cfg, sub_escaped)

    # -progress pipe:1 -nostats: xuất tiến độ máy-đọc-được ra stdout để vẽ thanh %
    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats"]
    # Giới hạn số luồng xử lý filter -> khống chế mức ăn CPU của mỗi video.
    # (Các hiệu ứng zoom/rung/tuyết/phụ đề chạy trên CPU; NVENC chỉ tăng tốc khâu nén.)
    ft = cfg.get("filter_threads")
    if ft:
        cmd += ["-filter_complex_threads", str(int(ft)),
                "-filter_threads", str(int(ft))]
    cmd += ["-loop", "1", "-i", img, "-i", audio]
    # THỨ TỰ input phải khớp overlay_indices(): snow trước, sóng âm sau.
    if cfg["effects"]["snow_overlay"]:
        cmd += ["-stream_loop", "-1", "-i", snow]
    if cfg["effects"].get("sound_wave"):
        cmd += ["-stream_loop", "-1", "-i", wave]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",
        "-r", str(cfg["fps"]),
        "-shortest",
    ]
    cmd += video_encode_args(cfg.get("encoder", "libx264"), cfg)
    cmd += ["-c:a", "aac", "-b:a", "192k", out]

    return {"ok": True, "cmd": cmd, "output": out, "missing": None,
            "duration": probe_duration(audio), "temp_sub": temp_sub}


def run_command(cmd, on_proc=None, on_progress=None, duration=None):
    """
    Thực thi lệnh ffmpeg bằng subprocess (KHÔNG dùng os.system).
    - on_proc(proc): callback nhận Popen để bên ngoài terminate khi cần (Dừng).
    - on_progress(frac): callback 0.0–1.0 tiến độ video (đọc từ luồng -progress).
    - duration: độ dài (giây) để quy đổi thời điểm hiện tại -> phần trăm.
    Trả về (returncode, stderr_text).

    stderr được rút cạn ở 1 thread riêng để tránh nghẽn pipe khi vừa đọc stdout
    (tiến độ) vừa nhận log lỗi.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_NO_WINDOW,
    )
    if on_proc:
        on_proc(proc)

    err_lines = []

    def _drain_err():
        try:
            for line in proc.stderr:
                err_lines.append(line)
        except Exception:  # noqa: BLE001
            pass

    th = threading.Thread(target=_drain_err, daemon=True)
    th.start()

    if on_progress and duration and duration > 0:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                val = line.split("=", 1)[1]
                try:
                    # out_time_us: micro giây; out_time_ms trong ffmpeg cũng là micro giây
                    us = int(val)
                except ValueError:
                    continue
                frac = us / 1_000_000.0 / duration
                on_progress(max(0.0, min(frac, 0.999)))
            elif line == "progress=end":
                on_progress(1.0)
    else:
        # vẫn phải rút cạn stdout để tiến trình không nghẽn
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass

    proc.wait()
    th.join(timeout=3)
    return proc.returncode, "".join(err_lines)
