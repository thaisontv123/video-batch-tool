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
import json
import time
import shutil
import random
import hashlib
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
            "scenery_bg": False,      # nền video phong cảnh (mờ) + ảnh chính đè giữa
            "glow_subtitle": False,
        },
        "scenery_dir": "",       # thư mục chứa các video phong cảnh (chọn qua UI)
        "scenery_image_scale": 0.62,   # cỡ ảnh chính so với chiều ngang khung
        "scenery_blur": 0,       # độ mờ nền (0 = giữ nền sắc nét, không làm mờ)
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


SCENERY_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def list_scenery(folder):
    """Danh sách video phong cảnh trong thư mục (sắp xếp theo tên)."""
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith(SCENERY_EXT):
            out.append(os.path.join(folder, f))
    return out


def _scenery_cache_dir():
    d = os.path.join(tempfile.gettempdir(), "vbt_scenery_cache")
    os.makedirs(d, exist_ok=True)
    # dọn file .tmp dở dang từ những lần chạy bị dừng giữa chừng (cũ hơn 1 giờ)
    now = time.time()
    for f in os.listdir(d):
        if f.endswith(".tmp.mp4"):
            p = os.path.join(d, f)
            try:
                if now - os.path.getmtime(p) > 3600:
                    os.remove(p)
            except OSError:
                pass
    return d


# Cache độ dài clip đã chuẩn hóa (trong 1 phiên chạy) -> tránh gọi ffprobe lặp lại
# cho mỗi project khi render hàng loạt.
_scenery_dur_cache = {}

# Cache thông tin video nguồn: (path, mtime, size) -> dict(duration, codec, w, h, fps, pix_fmt)
_probe_cache = {}


def probe_video_info(path):
    """Đọc thông số luồng video (codec/kích thước/fps/pix_fmt) + độ dài. Có cache."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if key in _probe_cache:
        return _probe_cache[key]
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,r_frame_rate,pix_fmt",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=30)
        data = json.loads(r.stdout or "{}")
        s = (data.get("streams") or [{}])[0]
        info = {
            "duration": float(data.get("format", {}).get("duration") or 0),
            "codec": s.get("codec_name"), "w": s.get("width"), "h": s.get("height"),
            "fps": s.get("r_frame_rate"), "pix_fmt": s.get("pix_fmt"),
        }
    except Exception:  # noqa: BLE001
        return None
    if not info["duration"] or not info["w"]:
        return None
    _probe_cache[key] = info
    return info


def pick_scenery_sources(files, duration, seed=None, margin=3.0):
    """Xáo trộn rồi chọn clip nguồn cho tổng độ dài >= duration (+ dư 1 chút).

    Đọc thông tin LƯỜI: chỉ ffprobe đúng những clip được chọn — thư mục 97 clip x 20
    phút mà voice 34 phút thì chỉ đọc 2 clip. Hết clip mà chưa đủ thì xáo lại & lặp.
    Trả về list [(path, info)].
    """
    rnd = random.Random(seed)
    order = files[:]
    rnd.shuffle(order)
    target = (duration or 0) + margin
    usable, seq, total, i = [], [], 0.0, 0
    while total < target and len(seq) < 20000:
        if i < len(order):
            p = order[i]
            i += 1
            info = probe_video_info(p)
            if not info:
                continue
            usable.append((p, info))
            seq.append((p, info))
            total += info["duration"]
        else:
            if not usable:
                break
            again = usable[:]
            rnd.shuffle(again)
            for item in again:
                seq.append(item)
                total += item[1]["duration"]
                if total >= target:
                    break
    return seq


def scenery_sources_compatible(seq):
    """Các clip có cùng codec/kích thước/fps/pix_fmt -> concat thẳng, KHÔNG cần encode lại."""
    keys = {(i["codec"], i["w"], i["h"], i["fps"], i["pix_fmt"]) for _, i in seq}
    return len(keys) == 1


def write_concat_list(paths):
    """Ghi file list cho concat demuxer, trả về đường dẫn (file tạm)."""
    fd, lst = tempfile.mkstemp(suffix=".txt", prefix="vbt_scenery_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for p in paths:
            safe = p.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    return lst


def normalize_scenery(files, res, fps, encoder):
    """Chuẩn hóa các video phong cảnh về cùng độ phân giải/fps/codec (để concat mượt).

    Chỉ dùng khi các clip ĐƯỢC CHỌN lệch thông số nhau. Kết quả CACHE theo (đường dẫn
    + mtime + size + res + fps) nên chỉ encode 1 lần.
    Trả về dict {src: (path_chuẩn_hóa, duration_giây)}.
    """
    cache = _scenery_cache_dir()
    out = {}
    for src in files:
        try:
            st = os.stat(src)
        except OSError:
            continue
        key = hashlib.md5(
            f"{os.path.abspath(src)}|{st.st_mtime_ns}|{st.st_size}|{res}|{fps}"
            .encode("utf-8")).hexdigest()
        dst = os.path.join(cache, key + ".mp4")
        if not os.path.exists(dst):
            vf = (f"scale={res}:force_original_aspect_ratio=increase,"
                  f"crop={res},fps={fps},setsar=1,format=yuv420p")
            # ghi ra file tạm riêng rồi đổi tên (nguyên tử) -> an toàn khi nhiều
            # project render song song cùng chuẩn hóa 1 clip.
            tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.tmp.mp4"
            cmd = ["ffmpeg", "-y", "-hwaccel", "auto", "-i", src, "-vf", vf, "-an"]
            cmd += video_encode_args(encoder, {"crf": 23, "preset": "medium"})
            cmd += [tmp]
            r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
            if r.returncode == 0 and os.path.exists(tmp):
                try:
                    os.replace(tmp, dst)
                except OSError:
                    pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not os.path.exists(dst):
                continue
        d = _scenery_dur_cache.get(dst)
        if d is None:
            d = probe_duration(dst)
            if d:
                _scenery_dur_cache[dst] = d
        if d:
            out[src] = (dst, d)
    return out


def input_layout(cfg):
    """Chỉ số input ffmpeg của TẤT CẢ nguồn, theo đúng thứ tự prepare_command thêm.

    - Chế độ thường:      [0]=ảnh, [1]=audio, [2]=snow?, [next]=sóng âm?
    - Chế độ nền phong cảnh: [0]=scenery(concat), [1]=ảnh, [2]=audio, [3]=snow?, ...
    Trả về dict: scenery/image/audio/snow/wave (None nếu tắt) + scenery_on (bool).
    """
    eff = cfg.get("effects", {})
    scenery_on = bool(eff.get("scenery_bg"))
    idx = {"scenery": None, "snow": None, "wave": None, "scenery_on": scenery_on}
    i = 0
    if scenery_on:
        idx["scenery"] = i
        i += 1
    idx["image"] = i
    i += 1
    idx["audio"] = i
    i += 1
    if eff.get("snow_overlay"):
        idx["snow"] = i
        i += 1
    if eff.get("sound_wave"):
        idx["wave"] = i
        i += 1
    return idx


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
def scenery_use_cuda(cfg, force_cpu=False):
    """Có dùng pipeline GPU (overlay_cuda) cho nền phong cảnh không?
    Điều kiện: bật nền phong cảnh + encoder NVENC (có NVIDIA) + không làm mờ nền."""
    if force_cpu or not cfg.get("effects", {}).get("scenery_bg"):
        return False
    return (cfg.get("encoder") == "h264_nvenc"
            and float(cfg.get("scenery_blur", 0) or 0) <= 0)


def _snow_layer(snow_idx, res, fps, opacity, out):
    """Lớp tuyết dạng TRẮNG + ALPHA (alpha = độ sáng hạt tuyết × opacity), yuva420p.

    Phủ màu trắng với alpha b = độ sáng/255 cho ra đúng công thức RGB screen
    (X' = X + b·(255 − X)) cho mọi kênh, kể cả chroma -> giống hệt blend screen cũ
    (đã đo PSNR 48 dB) nhưng KHÔNG phải đổi cả khung sang RGB (gbrp) và chạy được trên
    GPU bằng overlay_cuda. Chỉ đúng cho tuyết trắng/xám (đúng loại file snow).
    """
    w, h = res.split(":")
    # Thứ tự PHẢI là extractplanes -> scale (đặt scale trước extractplanes làm hỏng
    # chuỗi lọc: không ra khung nào — đã test). Scale 1 kênh xám cũng nhẹ hơn.
    return (f"[{snow_idx}:v]extractplanes=y,scale={w}:{h},"
            f"lut=c0='clip((val-16)*255/219*{opacity},0,255)'[{out}m];"
            f"color=c=white:s={w}x{h}:r={fps},format=yuva420p[{out}w];"
            f"[{out}w][{out}m]alphamerge[{out}]")


def build_filter_complex(cfg, sub_path, force_cpu_scenery=False):
    res = cfg["resolution"]
    w, h = res.split(":")
    effects = cfg["effects"]
    idx = input_layout(cfg)
    filter_parts = []
    snow_done = False
    snow_op = cfg.get("snow_opacity", 0.6)

    if idx["scenery_on"]:
        # CHẾ ĐỘ NỀN PHONG CẢNH: video phong cảnh phủ kín làm nền, ảnh chính đè giữa.
        # (Ảnh chính đứng yên — không áp hiệu ứng lắc ở chế độ này.)
        img_scale = float(cfg.get("scenery_image_scale", 0.62))
        iw = max(2, (int(int(w) * img_scale)) // 2 * 2)  # chẵn cho yuv420
        if scenery_use_cuda(cfg, force_cpu_scenery):
            # GPU: video nền đã chuẩn hóa đúng res + giải mã thẳng lên GPU (cuda frames);
            # ghép ảnh chính bằng overlay_cuda (trên GPU) -> giảm mạnh tải CPU. Chỉ tải
            # ảnh chính (nhỏ) lên GPU; sau overlay thì tải về CPU cho phụ đề (libass).
            gpu_snow = idx["snow"] is not None
            if gpu_snow:
                # overlay_cuda KHÔNG phủ được lớp alpha (yuva420p) lên nv12 -> đổi nền
                # sang yuv420p ngay trên GPU trước.
                filter_parts.append(f"[{idx['scenery']}:v]scale_cuda=format=yuv420p[scy]")
                main, fmt = "[scy]", "yuv420p"
            else:
                main, fmt = f"[{idx['scenery']}:v]", "nv12"
            filter_parts.append(
                f"[{idx['image']}:v]scale={iw}:-2,format={fmt},hwupload_cuda[fg]")
            filter_parts.append(f"{main}[fg]overlay_cuda=(W-w)/2:(H-h)/2-60[compc]")
            if gpu_snow:
                # Tuyết ghép trên GPU (chỉ tạo mặt nạ alpha trên CPU).
                filter_parts.append(
                    _snow_layer(idx["snow"], res, cfg["fps"], snow_op, "sna"))
                filter_parts.append("[sna]hwupload_cuda[snc]")
                filter_parts.append("[compc][snc]overlay_cuda=0:0[compc2]")
                filter_parts.append("[compc2]hwdownload,format=yuv420p[bg]")
                snow_done = True
            else:
                filter_parts.append("[compc]hwdownload,format=nv12[bg]")
        else:
            # CPU: chỉ làm mờ nếu scenery_blur > 0 (mặc định 0 = giữ nền SẮC NÉT).
            blur = float(cfg.get("scenery_blur", 0) or 0)
            bg_steps = (f"[{idx['scenery']}:v]scale={res}:"
                        f"force_original_aspect_ratio=increase,crop={res}")
            if blur > 0:
                bg_steps += f",gblur=sigma={blur}"
            filter_parts.append(f"{bg_steps}[bg0]")
            filter_parts.append(f"[{idx['image']}:v]scale={iw}:-2[fg]")
            # đặt ảnh hơi cao hơn giữa để chừa chỗ cho phụ đề bên dưới
            filter_parts.append("[bg0][fg]overlay=(W-w)/2:(H-h)/2-60[bg]")
        last_label = "[bg]"
    else:
        steps = [f"[{idx['image']}:v]scale={res}:"
                 f"force_original_aspect_ratio=increase,crop={res}"]
        if effects.get("smooth_shake"):
            # LẮC mượt theo quỹ đạo PARABOL (vòng cung), không trượt thẳng ngang:
            #   - Ngang (x): 1 sóng sin CHẬM chu kỳ 8s -> trôi trái<->phải như con lắc.
            #   - Dọc (y): dao động tần số GẤP ĐÔI (chu kỳ 4s) -> y ~ x² -> vẽ CUNG CONG.
            # velocity=0 ở 2 đầu nên đổi chiều êm; không sóng tần số cao -> không giật.
            s = int(cfg.get("smooth_shake_strength", 20))
            xexpr = f"{s}+{s}*0.90*sin(2*PI*t/8.0)"
            yexpr = f"{s}-{s}*0.35*cos(2*PI*t/4.0)"
            # SUPERSAMPLING chống GIẬT: crop chỉ dịch nguyên pixel -> lắc chậm nhảy
            # 0,1,0,1 px. Phóng lên Fx (bilinear) rồi crop-pan, thu nhỏ về res (bicubic)
            # -> vị trí lẻ pixel được nội suy mượt. Fx do UI "Độ mượt": Thường=2, Cao=3.
            F = max(1, int(cfg.get("shake_supersample", 2)))
            steps.append(f"scale=iw*{F}:ih*{F}:flags=bilinear")
            steps.append(
                f"crop=in_w-{s*2*F}:in_h-{s*2*F}:x='({xexpr})*{F}':y='({yexpr})*{F}'")
            steps.append(f"scale={res}:flags=bicubic")
        filter_parts.append(",".join(steps) + "[bg]")
        last_label = "[bg]"

    # Tuyết (nếu chưa ghép trên GPU): phủ trắng + alpha trên CPU, ngay trong yuv420p
    # (nhẹ hơn ~55% so với đổi cả khung sang RGB để blend screen).
    if idx["snow"] is not None and not snow_done:
        filter_parts.append(_snow_layer(idx["snow"], res, cfg["fps"], snow_op, "sna"))
        filter_parts.append(f"{last_label}[sna]overlay=0:0:format=yuv420[bgs]")
        last_label = "[bgs]"

    # Sóng âm (có MÀU) vẫn chồng bằng blend 'screen'. QUAN TRỌNG: phải chạy trong RGB
    # (gbrp) — nếu để YUV, phép screen tác động lên 2 kênh chroma Cb/Cr (trung tính 128)
    # đẩy lên ~192 -> ám HỒNG/tím toàn khung. Convert sang gbrp thì nền đen mới "biến mất".
    overlays = []
    if idx["wave"] is not None:
        overlays.append((idx["wave"], cfg.get("wave_opacity", 0.9)))

    if overlays:
        filter_parts.append(f"{last_label}format=gbrp[bgrgb]")
        last_label = "[bgrgb]"
        for i, (oidx, opacity) in enumerate(overlays):
            filter_parts.append(f"[{oidx}:v]scale={res},format=gbrp[ov{i}s]")
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
    scenery_on = bool(cfg["effects"].get("scenery_bg"))
    scenery_files = list_scenery(cfg.get("scenery_dir", "")) if scenery_on else []
    out = os.path.join(folder, cfg["output_name"])

    missing = [name for name, f in
               [("ảnh", img), ("audio", audio), ("phụ đề .srt", sub)] if f is None]
    if cfg["effects"]["snow_overlay"] and not (snow and os.path.exists(snow)):
        missing.append("snow.mp4")
    if cfg["effects"].get("sound_wave") and not (wave and os.path.exists(wave)):
        missing.append("file sóng âm")
    if scenery_on and not scenery_files:
        missing.append("video phong cảnh")
    if missing:
        return {"ok": False, "cmd": None, "output": out,
                "missing": f"Thiếu {', '.join(missing)}", "duration": None,
                "temp_sub": None, "temp_list": None}

    duration = probe_duration(audio)

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
    idx = input_layout(cfg)

    # Chế độ nền phong cảnh: xáo trộn + chọn đủ clip cho độ dài voice (chỉ đọc clip được
    # chọn). Clip cùng thông số -> ghép thẳng, KHÔNG encode lại; lệch -> chuẩn hóa riêng
    # những clip được chọn.
    temp_list = None
    scenery_res_ok = True   # video nền đúng độ phân giải output -> dùng được GPU overlay
    if scenery_on:
        seq = pick_scenery_sources(scenery_files, duration or 0)
        if not seq:
            return {"ok": False, "cmd": None, "output": out,
                    "missing": "Không đọc được video phong cảnh nào",
                    "duration": None, "temp_sub": temp_sub, "temp_list": None}
        if scenery_sources_compatible(seq):
            w0, h0 = seq[0][1]["w"], seq[0][1]["h"]
            scenery_res_ok = f"{w0}:{h0}" == cfg["resolution"]
            temp_list = write_concat_list([p for p, _ in seq])
        else:
            uniq = list(dict.fromkeys(p for p, _ in seq))
            norm = normalize_scenery(uniq, cfg["resolution"], cfg["fps"],
                                     cfg.get("encoder", "libx264"))
            paths = [norm[p][0] for p, _ in seq if p in norm]
            if not paths:
                return {"ok": False, "cmd": None, "output": out,
                        "missing": "Không chuẩn hóa được video phong cảnh",
                        "duration": None, "temp_sub": temp_sub, "temp_list": None}
            temp_list = write_concat_list(paths)

    ft = cfg.get("filter_threads")

    def assemble(filter_complex, scenery_hwaccel):
        # scenery_hwaccel: list cờ -hwaccel đặt TRƯỚC input scenery.
        c = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats"]
        if ft:
            c += ["-filter_complex_threads", str(int(ft)),
                  "-filter_threads", str(int(ft))]
        # THỨ TỰ input PHẢI khớp input_layout(): [scenery], ảnh, audio, [snow], [sóng âm].
        if scenery_on and temp_list:
            c += scenery_hwaccel + ["-f", "concat", "-safe", "0", "-i", temp_list]
        if scenery_on:
            # Nền phong cảnh: ảnh chính chỉ là lớp đè -> nạp 1 khung DUY NHẤT (không
            # -loop), overlay tự giữ khung cuối suốt video. Dùng -loop sẽ giải mã lại
            # PNG + scale + đẩy lên GPU ở MỌI khung -> tốn ~4.5x CPU (đã đo).
            c += ["-i", img, "-i", audio]
        else:
            c += ["-loop", "1", "-i", img, "-i", audio]
        if cfg["effects"]["snow_overlay"]:
            # giải mã file tuyết trên GPU (auto: không có GPU thì tự về CPU)
            c += ["-hwaccel", "auto", "-stream_loop", "-1", "-i", snow]
        if cfg["effects"].get("sound_wave"):
            c += ["-stream_loop", "-1", "-i", wave]
        c += ["-filter_complex", filter_complex,
              "-map", "[vout]", "-map", f"{idx['audio']}:a",
              "-r", str(cfg["fps"]), "-shortest"]
        c += video_encode_args(cfg.get("encoder", "libx264"), cfg)
        c += ["-c:a", "aac", "-b:a", "192k", out]
        return c

    # GPU overlay cần video nền đúng độ phân giải output (pipeline GPU không scale nền).
    use_cuda = scenery_use_cuda(cfg) and scenery_res_ok
    fallback_cmd = None
    if use_cuda:
        # Lệnh chính: giải mã video nền THẲNG lên GPU (cuda) + overlay_cuda ghép ảnh.
        cmd = assemble(build_filter_complex(cfg, sub_escaped),
                       ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
        # Lệnh DỰ PHÒNG: nếu GPU lỗi -> giải mã GPU (auto) rồi ghép ảnh trên CPU.
        fallback_cmd = assemble(
            build_filter_complex(cfg, sub_escaped, force_cpu_scenery=True),
            ["-hwaccel", "auto"])
    else:
        # -hwaccel auto: giải mã video nền trên GPU (NVDEC/DXVA) rồi xử lý CPU. Máy
        # không có GPU giải mã thì tự về CPU (an toàn mọi máy).
        cmd = assemble(build_filter_complex(cfg, sub_escaped, force_cpu_scenery=True),
                       ["-hwaccel", "auto"] if scenery_on else [])

    return {"ok": True, "cmd": cmd, "fallback_cmd": fallback_cmd, "output": out,
            "missing": None, "duration": duration,
            "temp_sub": temp_sub, "temp_list": temp_list}


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
