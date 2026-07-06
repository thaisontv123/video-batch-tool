"""
Auto Batch Video Render Tool — Giao diện người dùng (Tkinter).

Chạy:  python app.py    (hoặc pythonw app.py để không hiện console)

Toàn bộ logic ffmpeg nằm ở render_core.py và được giữ nguyên theo bản CLI đã test.
GUI chỉ thu thập cấu hình từ người dùng -> dựng CONFIG -> render song song có tiến trình.
"""

import os
import queue
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, font as tkfont

import render_core as core

APP_TITLE = "Công cụ Render Video Hàng Loạt"

# Danh sách font phổ biến ưu tiên đưa lên đầu dropdown (nếu có trên máy)
COMMON_FONTS = [
    "Arial", "Roboto", "Montserrat", "Be Vietnam Pro", "Segoe UI",
    "Times New Roman", "Tahoma", "Verdana", "Calibri", "Open Sans",
]

# Trạng thái hiển thị
ST_WAIT = "⏳ Đang chờ"
ST_RUN = "🔄 Đang render"
ST_OK = "✅ Thành công"
ST_ERR = "❌ Lỗi"
ST_SKIP = "⚠️ Bỏ qua"
ST_CANCEL = "⛔ Đã hủy"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x780")
        self.minsize(880, 680)

        # --- state ---
        self.root_dir = tk.StringVar()
        self.snow_path = tk.StringVar()
        self.projects = []                 # danh sách đường dẫn thư mục con
        self.row_by_folder = {}            # folder -> item id trong Treeview
        self.row_data = {}                 # folder -> dict(name,status,progress,note)
        self.frac = {}                     # folder -> tiến độ 0..1 (cho thanh tổng)
        self.error_logs = {}               # folder -> log lỗi đầy đủ
        self.msg_queue = queue.Queue()     # hàng đợi cập nhật từ worker -> UI
        self.active_procs = {}             # folder -> Popen (để hủy)
        self.procs_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.is_rendering = False
        self.done_count = 0
        self.total_count = 0

        # --- effect vars ---
        self.snow_on = tk.BooleanVar(value=True)
        self.snow_opacity = tk.DoubleVar(value=0.6)
        self.smooth_shake_on = tk.BooleanVar(value=True)
        self.smooth_shake_strength = tk.IntVar(value=20)
        # Độ mượt lắc (supersampling): nhãn -> hệ số F
        self.quality_choices = {"Thường (nhanh)": 2, "Cao (mượt hơn, chậm)": 3}
        self.smooth_quality = tk.StringVar(value="Thường (nhanh)")
        self.glow_on = tk.BooleanVar(value=False)

        # --- subtitle vars ---
        self.font_name = tk.StringVar(value="Arial")
        self.font_size = tk.IntVar(value=22)
        self.font_color = tk.StringVar(value="#FFFFFF")
        self.outline_color = tk.StringVar(value="#000000")
        self.outline_width = tk.IntVar(value=2)
        self.bold_on = tk.BooleanVar(value=False)
        self.sub_margin_v = tk.IntVar(value=10)   # lề dọc phụ đề (thang ASS ~288)

        # --- output vars ---
        self.resolution = tk.StringVar(value="1080p")
        self.fps = tk.IntVar(value=24)
        self.crf = tk.IntVar(value=20)
        _cpu = os.cpu_count() or 4
        # Chế độ chạy: "sequential" (lần lượt) | "parallel" (song song)
        self.run_mode = tk.StringVar(value="parallel")
        # Mặc định nhẹ nhàng (~50% CPU) để không chiếm hết máy — hiệu ứng luôn chạy CPU.
        # Tổng luồng ≈ video_song_song × luồng/video ≈ nửa số nhân.
        self.max_workers = tk.IntVar(value=max(2, _cpu // 4))
        self.filter_threads = tk.IntVar(value=2)

        # --- encoder (CPU/GPU) ---
        self.encoder_label = tk.StringVar(value=core.CPU_ENCODER["label"])
        self.encoder_choices = {core.CPU_ENCODER["label"]: core.CPU_ENCODER["key"]}

        self._build_ui()
        self._check_ffmpeg_on_start()
        self.after(120, self._poll_queue)
        self._detect_encoders_async()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # cuộn được toàn bộ khu vực cấu hình
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        # Cột trái: cấu hình (scroll) | Cột phải: tiến trình
        paned = ttk.PanedWindow(outer, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        left_container = ttk.Frame(paned)
        right_container = ttk.Frame(paned)
        paned.add(left_container, weight=3)
        paned.add(right_container, weight=4)

        self._build_scrollable_config(left_container)
        self._build_progress_panel(right_container)

    def _build_scrollable_config(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.cfg_frame = ttk.Frame(canvas)
        self.cfg_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        win = canvas.create_window((0, 0), window=self.cfg_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # cuộn bằng con lăn chuột
        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _wheel)

        self._build_folder_section(self.cfg_frame)
        self._build_effects_section(self.cfg_frame)
        self._build_subtitle_section(self.cfg_frame)
        self._build_output_section(self.cfg_frame)
        self._build_action_buttons(self.cfg_frame)

    def _section(self, parent, title):
        lf = ttk.LabelFrame(parent, text=title, padding=10)
        lf.pack(fill="x", padx=8, pady=6)
        return lf

    # --- 1. Thư mục gốc ---
    def _build_folder_section(self, parent):
        f = self._section(parent, "📁 Thư mục gốc")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.root_dir, state="readonly").pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Chọn...", command=self._pick_root).pack(
            side="left", padx=(6, 0))
        self.lbl_projects = ttk.Label(f, text="Chưa chọn thư mục.", foreground="#555")
        self.lbl_projects.pack(anchor="w", pady=(6, 0))

    # --- 2. Hiệu ứng ---
    def _build_effects_section(self, parent):
        f = self._section(parent, "✨ Hiệu ứng")

        # Snow
        r = ttk.Frame(f); r.pack(fill="x", pady=2)
        ttk.Checkbutton(r, text="❄️  Tuyết rơi (Snow overlay)",
                        variable=self.snow_on).pack(side="left")
        self._slider(f, "Độ mờ tuyết (opacity)", self.snow_opacity,
                     0.0, 1.0, resolution=0.05)
        sr = ttk.Frame(f); sr.pack(fill="x", pady=(0, 4))
        ttk.Label(sr, text="File snow.mp4:").pack(side="left")
        ttk.Entry(sr, textvariable=self.snow_path, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(sr, text="Chọn...", command=self._pick_snow).pack(side="left")

        ttk.Separator(f).pack(fill="x", pady=6)

        # Lắc nhẹ sang 2 bên (mượt) — thay cho camera shake + motion zoom cũ
        ttk.Checkbutton(f, text="🎞️  Lắc nhẹ sang 2 bên (mượt như con lắc)",
                        variable=self.smooth_shake_on).pack(anchor="w", pady=2)
        self._slider(f, "Độ rộng lắc (px)", self.smooth_shake_strength, 6, 50,
                     resolution=1, is_int=True)
        rq = ttk.Frame(f); rq.pack(fill="x", pady=(2, 0))
        ttk.Label(rq, text="Độ mượt:", width=20).pack(side="left")
        ttk.Combobox(rq, textvariable=self.smooth_quality,
                     values=list(self.quality_choices.keys()), state="readonly",
                     width=20).pack(side="left")

        ttk.Separator(f).pack(fill="x", pady=6)

        # Glow subtitle
        ttk.Checkbutton(f, text="🌟  Phụ đề phát sáng (Glow subtitle)",
                        variable=self.glow_on).pack(anchor="w", pady=2)

    # --- 3. Phụ đề ---
    def _build_subtitle_section(self, parent):
        f = self._section(parent, "🔤 Phụ đề (Subtitle)")

        r1 = ttk.Frame(f); r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="Font chữ:", width=14).pack(side="left")
        self.font_combo = ttk.Combobox(r1, textvariable=self.font_name,
                                        values=self._font_list(), state="readonly")
        self.font_combo.pack(side="left", fill="x", expand=True)

        r2 = ttk.Frame(f); r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="Cỡ chữ:", width=14).pack(side="left")
        ttk.Spinbox(r2, from_=10, to=80, textvariable=self.font_size,
                    width=6).pack(side="left")
        ttk.Checkbutton(r2, text="In đậm (Bold)", variable=self.bold_on).pack(
            side="left", padx=16)

        r3 = ttk.Frame(f); r3.pack(fill="x", pady=3)
        ttk.Label(r3, text="Màu chữ:", width=14).pack(side="left")
        self.sw_font = tk.Label(r3, textvariable=self.font_color, width=12,
                                relief="solid", bd=1)
        self.sw_font.pack(side="left")
        ttk.Button(r3, text="Chọn màu",
                   command=lambda: self._pick_color(self.font_color, self.sw_font)
                   ).pack(side="left", padx=6)

        r4 = ttk.Frame(f); r4.pack(fill="x", pady=3)
        ttk.Label(r4, text="Màu viền:", width=14).pack(side="left")
        self.sw_outline = tk.Label(r4, textvariable=self.outline_color, width=12,
                                   relief="solid", bd=1)
        self.sw_outline.pack(side="left")
        ttk.Button(r4, text="Chọn màu",
                   command=lambda: self._pick_color(self.outline_color, self.sw_outline)
                   ).pack(side="left", padx=6)

        r5 = ttk.Frame(f); r5.pack(fill="x", pady=3)
        ttk.Label(r5, text="Độ dày viền:", width=14).pack(side="left")
        ttk.Spinbox(r5, from_=0, to=10, textvariable=self.outline_width,
                    width=6).pack(side="left")

        # Vị trí dọc phụ đề: số nhỏ = sát đáy, lớn = nhấc cao hơn (thang ASS ~288).
        r6 = ttk.Frame(f); r6.pack(fill="x", pady=3)
        ttk.Label(r6, text="Vị trí (lề dưới):", width=14).pack(side="left")
        ttk.Spinbox(r6, from_=0, to=120, increment=2,
                    textvariable=self.sub_margin_v, width=6).pack(side="left")
        ttk.Label(r6, text="nhỏ = sát đáy, lớn = cao hơn (~10 khớp mẫu)",
                  foreground="#777").pack(side="left", padx=8)

        self._refresh_swatches()

    # --- 4. Xuất video ---
    def _build_output_section(self, parent):
        f = self._section(parent, "🎬 Xuất video")

        # Bộ mã hóa CPU/GPU (dò tự động khi mở app)
        re = ttk.Frame(f); re.pack(fill="x", pady=3)
        ttk.Label(re, text="Bộ mã hóa:", width=16).pack(side="left")
        self.encoder_combo = ttk.Combobox(
            re, textvariable=self.encoder_label,
            values=list(self.encoder_choices.keys()), state="readonly", width=22)
        self.encoder_combo.pack(side="left")
        self.lbl_encoder = ttk.Label(re, text="(đang dò GPU...)", foreground="#777")
        self.lbl_encoder.pack(side="left", padx=8)

        ttk.Label(f, text="ℹ Hiệu ứng (rung/tuyết/phụ đề) luôn xử lý trên CPU; "
                          "GPU chỉ tăng tốc khâu nén video.",
                  foreground="#777", wraplength=340, justify="left").pack(
            anchor="w", pady=(0, 4))

        r1 = ttk.Frame(f); r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="Độ phân giải:", width=16).pack(side="left")
        ttk.Combobox(r1, textvariable=self.resolution,
                     values=list(core.RESOLUTIONS.keys()), state="readonly",
                     width=10).pack(side="left")

        r2 = ttk.Frame(f); r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="FPS:", width=16).pack(side="left")
        ttk.Spinbox(r2, from_=1, to=60, textvariable=self.fps, width=6).pack(side="left")

        # CRF slider có nhãn "Nét hơn <-> Nhẹ hơn"
        r3 = ttk.Frame(f); r3.pack(fill="x", pady=3)
        ttk.Label(r3, text="Chất lượng (CRF):", width=16).pack(side="left")
        self.crf_val = ttk.Label(r3, text=str(self.crf.get()), width=4)
        self.crf_val.pack(side="right")
        sc = ttk.Scale(r3, from_=18, to=28, orient="horizontal",
                       command=lambda v: (self.crf.set(round(float(v))),
                                          self.crf_val.config(text=str(self.crf.get()))))
        sc.set(self.crf.get())
        sc.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(f, text="◀ Nét hơn (nặng)          Nhẹ hơn (mờ) ▶",
                  foreground="#777").pack(anchor="center")

        cpu = os.cpu_count() or 8

        # Chế độ chạy: lần lượt / song song
        rm = ttk.Frame(f); rm.pack(fill="x", pady=(6, 1))
        ttk.Label(rm, text="Chế độ chạy:", width=16).pack(side="left")
        ttk.Radiobutton(rm, text="Lần lượt", value="sequential",
                        variable=self.run_mode,
                        command=self._on_mode_change).pack(side="left")
        ttk.Radiobutton(rm, text="Song song", value="parallel",
                        variable=self.run_mode,
                        command=self._on_mode_change).pack(side="left", padx=(10, 4))
        self.sp_parallel = ttk.Spinbox(rm, from_=2, to=cpu,
                                       textvariable=self.max_workers, width=5,
                                       command=self._update_cpu_hint)
        self.sp_parallel.pack(side="left")
        self.sp_parallel.bind("<KeyRelease>", lambda e: self._update_cpu_hint())
        ttk.Label(rm, text="video/lần").pack(side="left", padx=(4, 0))

        r5 = ttk.Frame(f); r5.pack(fill="x", pady=3)
        ttk.Label(r5, text="Luồng CPU/video:", width=16).pack(side="left")
        sp2 = ttk.Spinbox(r5, from_=1, to=cpu, textvariable=self.filter_threads,
                          width=6, command=self._update_cpu_hint)
        sp2.pack(side="left")
        sp2.bind("<KeyRelease>", lambda e: self._update_cpu_hint())
        ttk.Label(r5, text="← giảm để CPU nhẹ hơn", foreground="#777").pack(
            side="left", padx=8)

        self.lbl_cpu_hint = ttk.Label(f, foreground="#777")
        self.lbl_cpu_hint.pack(anchor="w", pady=(2, 0))
        self._update_cpu_hint()

    def _effective_workers(self):
        if self.run_mode.get() == "sequential":
            return 1
        try:
            return max(1, int(self.max_workers.get()))
        except Exception:  # noqa: BLE001
            return 1

    def _on_mode_change(self):
        # bật/tắt ô nhập số video song song theo chế độ
        seq = self.run_mode.get() == "sequential"
        self.sp_parallel.config(state="disabled" if seq else "normal")
        self._update_cpu_hint()

    def _update_cpu_hint(self):
        cpu = os.cpu_count() or 8
        try:
            load = self._effective_workers() * int(self.filter_threads.get())
        except Exception:  # noqa: BLE001
            return
        pct = min(100, round(load / cpu * 100))
        level = "nhẹ" if pct <= 45 else ("vừa" if pct <= 80 else "nặng")
        mode = ("lần lượt (1 video/lần)" if self.run_mode.get() == "sequential"
                else f"song song {self._effective_workers()} video/lần")
        self.lbl_cpu_hint.config(
            text=f"Chế độ {mode} • máy {cpu} nhân — ước tính CPU: ~{pct}% ({level}).")

    # --- 5. Nút hành động ---
    def _build_action_buttons(self, parent):
        f = ttk.Frame(parent)
        f.pack(fill="x", padx=8, pady=10)
        self.btn_preview = ttk.Button(f, text="👁 Xem thử 1 video",
                                      command=self._on_preview)
        self.btn_preview.pack(side="left", padx=(0, 6))
        self.btn_render = ttk.Button(f, text="▶ Render tất cả",
                                     command=self._on_render_all)
        self.btn_render.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(f, text="■ Dừng", command=self._on_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=6)

    # --- Panel tiến trình (phải) ---
    def _build_progress_panel(self, parent):
        f = ttk.LabelFrame(parent, text="📊 Tiến trình render", padding=8)
        f.pack(fill="both", expand=True)

        cols = ("project", "status", "progress", "note")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        self.tree.heading("project", text="Project")
        self.tree.heading("status", text="Trạng thái")
        self.tree.heading("progress", text="Tiến độ")
        self.tree.heading("note", text="Ghi chú")
        self.tree.column("project", width=170, anchor="w")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("progress", width=150, anchor="w")
        self.tree.column("note", width=180, anchor="w")
        tsb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_row_double_click)

        bottom = ttk.Frame(parent)
        bottom.pack(fill="x", pady=(6, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.lbl_progress = ttk.Label(bottom, text="0/0 video hoàn thành", width=22)
        self.lbl_progress.pack(side="left", padx=8)

        ttk.Button(parent, text="📂 Mở thư mục output",
                   command=self._open_output).pack(anchor="e", pady=6)

    # -------------------------------------------------------------- helpers UI
    def _slider(self, parent, label, var, lo, hi, resolution=0.1,
                is_int=False, fmt=None):
        r = ttk.Frame(parent); r.pack(fill="x", pady=1)
        ttk.Label(r, text=label, width=20).pack(side="left")
        vallbl = ttk.Label(r, width=8)
        vallbl.pack(side="right")

        def fmt_val(v):
            if fmt:
                return fmt.format(float(v))
            return str(int(round(float(v)))) if is_int else f"{float(v):.2f}"

        def on_move(v):
            if is_int:
                var.set(int(round(float(v))))
            else:
                # snap theo resolution
                steps = round(float(v) / resolution)
                var.set(round(steps * resolution, 6))
            vallbl.config(text=fmt_val(var.get()))

        sc = ttk.Scale(r, from_=lo, to=hi, orient="horizontal", command=on_move)
        sc.set(var.get())
        sc.pack(side="left", fill="x", expand=True, padx=6)
        vallbl.config(text=fmt_val(var.get()))

    def _font_list(self):
        try:
            available = set(tkfont.families())
        except Exception:  # noqa: BLE001
            available = set()
        top = [f for f in COMMON_FONTS if f in available]
        rest = sorted(x for x in available if x not in set(top) and not x.startswith("@"))
        return top + rest if (top or rest) else COMMON_FONTS

    def _refresh_swatches(self):
        self.sw_font.config(bg=self.font_color.get(),
                            fg=self._contrast(self.font_color.get()))
        self.sw_outline.config(bg=self.outline_color.get(),
                               fg=self._contrast(self.outline_color.get()))

    @staticmethod
    def _contrast(hexc):
        try:
            h = hexc.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else "#FFFFFF"
        except Exception:  # noqa: BLE001
            return "#000000"

    # ------------------------------------------------------------- actions
    def _pick_root(self):
        d = filedialog.askdirectory(title="Chọn thư mục gốc chứa các project")
        if not d:
            return
        self.root_dir.set(d)
        # tự đoán snow.mp4 nếu nằm trong thư mục gốc
        guess = os.path.join(d, "snow.mp4")
        if os.path.exists(guess) and not self.snow_path.get():
            self.snow_path.set(guess)
        self._scan_projects()

    def _pick_snow(self):
        f = filedialog.askopenfilename(
            title="Chọn file snow.mp4",
            filetypes=[("Video", "*.mp4 *.mov *.mkv"), ("Tất cả", "*.*")])
        if f:
            self.snow_path.set(f)

    def _pick_color(self, var, swatch):
        rgb, hexc = colorchooser.askcolor(color=var.get(), title="Chọn màu")
        if hexc:
            var.set(hexc.upper())
            swatch.config(bg=hexc, fg=self._contrast(hexc))

    def _scan_projects(self):
        self.projects = core.find_projects(self.root_dir.get())
        # reset bảng
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.row_by_folder.clear()
        self.row_data.clear()
        self.frac.clear()
        self.error_logs.clear()

        need_snow = self.snow_on.get()
        snow = self.snow_path.get()
        ready = 0
        for folder in self.projects:
            name = os.path.basename(folder)
            missing = core.project_status(folder, need_snow, snow)
            if missing:
                status, note = ST_SKIP, f"Thiếu {', '.join(missing)}"
            else:
                status, note = ST_WAIT, ""
                ready += 1
            self.row_data[folder] = {"name": name, "status": status,
                                     "progress": "", "note": note}
            iid = self.tree.insert("", "end",
                                   values=(name, status, "", note))
            self.row_by_folder[folder] = iid

        self.lbl_projects.config(
            text=f"Tìm thấy {len(self.projects)} project — {ready} sẵn sàng render.")
        self.total_count = len(self.projects)
        self.done_count = 0
        self.progress.config(maximum=max(1, len(self.projects)), value=0)
        self.lbl_progress.config(text=f"0/{len(self.projects)} video hoàn thành")

    # ---------- build CONFIG từ UI ----------
    def _build_config(self):
        cfg = core.default_config()
        cfg["root_dir"] = self.root_dir.get()
        cfg["resolution"] = core.RESOLUTIONS.get(self.resolution.get(), "1920:1080")
        cfg["fps"] = int(self.fps.get())
        cfg["crf"] = int(self.crf.get())
        cfg["encoder"] = self.encoder_choices.get(self.encoder_label.get(), "libx264")
        cfg["snow_video_path"] = self.snow_path.get()
        cfg["snow_opacity"] = round(float(self.snow_opacity.get()), 3)
        cfg["smooth_shake_strength"] = int(self.smooth_shake_strength.get())
        cfg["shake_supersample"] = self.quality_choices.get(self.smooth_quality.get(), 2)
        cfg["max_workers"] = self._effective_workers()  # 1 nếu chạy lần lượt
        cfg["filter_threads"] = int(self.filter_threads.get())
        cfg["effects"] = {
            "snow_overlay": bool(self.snow_on.get()),
            "smooth_shake": bool(self.smooth_shake_on.get()),
            "glow_subtitle": bool(self.glow_on.get()),
        }
        cfg["subtitle"] = {
            "font_name": self.font_name.get(),
            "font_size": int(self.font_size.get()),
            "font_color": core.hex_to_ass(self.font_color.get()),
            "outline_color": core.hex_to_ass(self.outline_color.get()),
            "outline_width": int(self.outline_width.get()),
            "bold": bool(self.bold_on.get()),
            "alignment": 2,
            "margin_v": int(self.sub_margin_v.get()),
        }
        return cfg

    def _validate_before_render(self):
        if not self.root_dir.get():
            messagebox.showwarning(APP_TITLE, "Bạn chưa chọn thư mục gốc.")
            return False
        if not self.projects:
            messagebox.showwarning(APP_TITLE, "Không tìm thấy project nào trong thư mục.")
            return False
        if self.snow_on.get() and not (self.snow_path.get() and
                                       os.path.exists(self.snow_path.get())):
            messagebox.showwarning(
                APP_TITLE,
                "Đang bật hiệu ứng Tuyết nhưng chưa chọn file snow.mp4 hợp lệ.")
            return False
        ok, _ = core.check_ffmpeg()
        if not ok:
            messagebox.showerror(APP_TITLE, "Không tìm thấy ffmpeg. Vui lòng cài đặt trước.")
            return False
        return True

    def _on_preview(self):
        if self.is_rendering:
            return
        if not self._validate_before_render():
            return
        # chọn project sẵn sàng đầu tiên
        cfg = self._build_config()
        target = None
        for folder in self.projects:
            if not core.project_status(folder, cfg["effects"]["snow_overlay"],
                                       cfg["snow_video_path"]):
                target = folder
                break
        if not target:
            messagebox.showwarning(APP_TITLE, "Không có project nào đủ file để xem thử.")
            return
        self._start_render([target], cfg, preview=True)

    def _on_render_all(self):
        if self.is_rendering:
            return
        if not self._validate_before_render():
            return
        cfg = self._build_config()
        self._start_render(list(self.projects), cfg, preview=False)

    # ---------- điều phối render (threaded) ----------
    def _start_render(self, folders, cfg, preview=False):
        self.is_rendering = True
        self.cancel_event.clear()
        self.done_count = 0
        self.total_count = len(folders)
        self._set_controls_running(True)
        # thanh tổng chạy theo tổng phần trăm (mượt), không chỉ đếm số video
        self.progress.config(maximum=max(1, len(folders)), value=0)
        self.lbl_progress.config(text=f"0/{len(folders)} video hoàn thành")

        # đặt các dòng liên quan về trạng thái chờ + reset tiến độ
        self.frac = {f: 0.0 for f in folders}
        for folder in folders:
            self._set_row(folder, ST_WAIT, "", progress="")

        t = threading.Thread(target=self._render_worker,
                             args=(folders, cfg, preview), daemon=True)
        t.start()

    def _render_worker(self, folders, cfg, preview):
        def run_one(folder):
            if self.cancel_event.is_set():
                self._emit(folder, ST_CANCEL, "Đã hủy trước khi bắt đầu")
                return
            prep = core.prepare_command(folder, cfg)
            if not prep["ok"]:
                self._emit(folder, ST_SKIP, prep["missing"])
                return
            self._emit(folder, ST_RUN, "Đang xử lý...")

            def on_proc(p):
                with self.procs_lock:
                    self.active_procs[folder] = p

            last_pct = [-1]

            def on_progress(frac):
                pct = int(frac * 100)
                if pct != last_pct[0]:      # throttle: chỉ gửi khi đổi 1%
                    last_pct[0] = pct
                    self._emit_progress(folder, frac)

            try:
                code, stderr = core.run_command(
                    prep["cmd"], on_proc=on_proc,
                    on_progress=on_progress, duration=prep.get("duration"))
            except Exception as e:  # noqa: BLE001
                self._emit(folder, ST_ERR, f"Lỗi: {e}", log=str(e))
                return
            finally:
                with self.procs_lock:
                    self.active_procs.pop(folder, None)

            if self.cancel_event.is_set():
                self._emit(folder, ST_CANCEL, "Đã hủy")
                return
            if code == 0:
                self._emit(folder, ST_OK, os.path.basename(prep["output"]))
            else:
                tail = (stderr or "").strip()
                short = tail[-160:].replace("\n", " ") if tail else "ffmpeg lỗi"
                self._emit(folder, ST_ERR, short, log=tail)

        workers = 1 if preview else max(1, cfg["max_workers"])
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(run_one, f) for f in folders]
            for fut in futures:
                fut.result()  # đảm bảo hoàn tất

        self.msg_queue.put({"kind": "done", "preview": preview})

    def _on_cancel(self):
        if not self.is_rendering:
            return
        self.cancel_event.set()
        with self.procs_lock:
            for p in list(self.active_procs.values()):
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass
        self.btn_cancel.config(state="disabled")

    # ---------- giao tiếp worker -> UI qua queue ----------
    def _emit(self, folder, status, note, log=None):
        self.msg_queue.put({"kind": "row", "folder": folder, "status": status,
                            "note": note, "log": log})

    def _emit_progress(self, folder, frac):
        self.msg_queue.put({"kind": "prog", "folder": folder, "frac": frac})

    @staticmethod
    def _bar_text(frac, width=12):
        frac = max(0.0, min(frac, 1.0))
        filled = int(round(frac * width))
        return "█" * filled + "░" * (width - filled) + f" {int(frac * 100)}%"

    def _poll_queue(self):
        try:
            while True:
                m = self.msg_queue.get_nowait()
                kind = m["kind"]
                if kind == "done":
                    self._on_render_done(preview=m["preview"])
                elif kind == "encoders":
                    self._apply_encoders(m["list"])
                elif kind == "prog":
                    self._set_progress(m["folder"], m["frac"])
                elif kind == "row":
                    folder, status, note = m["folder"], m["status"], m["note"]
                    if m.get("log") is not None:
                        self.error_logs[folder] = m["log"]
                    if status == ST_RUN:
                        self._set_row(folder, status, note, progress=self._bar_text(0))
                    elif status == ST_OK:
                        self._set_row(folder, status, note,
                                      progress=self._bar_text(1.0))
                        self.frac[folder] = 1.0
                    else:  # ERR/SKIP/CANCEL
                        self._set_row(folder, status, note, progress="")
                        self.frac[folder] = 1.0
                    if status in (ST_OK, ST_ERR, ST_SKIP, ST_CANCEL):
                        self.done_count += 1
                        self.lbl_progress.config(
                            text=f"{self.done_count}/{self.total_count} video hoàn thành")
                    self._update_overall()
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _update_overall(self):
        """Thanh tổng = tổng phần trăm các video (mượt)."""
        if self.total_count:
            self.progress.config(value=sum(self.frac.get(f, 0.0)
                                           for f in self.frac))

    def _set_progress(self, folder, frac):
        self.frac[folder] = frac
        d = self.row_data.get(folder)
        if d and d["status"] == ST_RUN:
            self._set_row(folder, ST_RUN, d["note"], progress=self._bar_text(frac))
        self._update_overall()

    def _set_row(self, folder, status, note, progress=None):
        d = self.row_data.get(folder)
        if d is None:
            d = self.row_data[folder] = {"name": os.path.basename(folder),
                                         "status": status, "progress": "", "note": note}
        d["status"] = status
        d["note"] = note
        if progress is not None:
            d["progress"] = progress
        iid = self.row_by_folder.get(folder)
        if iid:
            self.tree.item(iid, values=(d["name"], d["status"],
                                        d["progress"], d["note"]))

    # ---------- dò encoder CPU/GPU chạy nền ----------
    def _detect_encoders_async(self):
        def work():
            try:
                encs = core.detect_encoders()
            except Exception:  # noqa: BLE001
                encs = [dict(core.CPU_ENCODER)]
            self.msg_queue.put({"kind": "encoders", "list": encs})
        threading.Thread(target=work, daemon=True).start()

    def _apply_encoders(self, encs):
        self.encoder_choices = {e["label"]: e["key"] for e in encs}
        labels = list(self.encoder_choices.keys())
        self.encoder_combo.config(values=labels)
        gpu = [e for e in encs if e["key"] != "libx264"]
        if gpu:
            self.encoder_label.set(gpu[0]["label"])  # mặc định GPU
            self.lbl_encoder.config(
                text=f"✅ Đã bật GPU: {gpu[0]['label']}", foreground="#127a12")
        else:
            self.encoder_label.set(core.CPU_ENCODER["label"])
            self.lbl_encoder.config(
                text="Không thấy GPU dùng được — dùng CPU", foreground="#a15c00")

    def _on_render_done(self, preview=False):
        self.is_rendering = False
        self._set_controls_running(False)
        if self.cancel_event.is_set():
            messagebox.showinfo(APP_TITLE, "Đã dừng render.")
        elif preview:
            messagebox.showinfo(
                APP_TITLE, "Xem thử xong! Kiểm tra file output.mp4 trong project.")
        else:
            ok = sum(1 for d in self.row_data.values() if d["status"] == ST_OK)
            messagebox.showinfo(
                APP_TITLE,
                f"Hoàn tất! {ok}/{self.total_count} video render thành công.")

    def _set_controls_running(self, running):
        state = "disabled" if running else "normal"
        self.btn_preview.config(state=state)
        self.btn_render.config(state=state)
        self.btn_cancel.config(state="normal" if running else "disabled")

    # ---------- misc ----------
    def _on_row_double_click(self, _event):
        item = self.tree.focus()
        if not item:
            return
        folder = next((f for f, i in self.row_by_folder.items() if i == item), None)
        if not folder:
            return
        log = self.error_logs.get(folder)
        if not log:
            messagebox.showinfo(APP_TITLE, "Không có log lỗi chi tiết cho project này.")
            return
        self._show_log_window(os.path.basename(folder), log)

    def _show_log_window(self, name, log):
        win = tk.Toplevel(self)
        win.title(f"Log lỗi — {name}")
        win.geometry("760x460")
        txt = tk.Text(win, wrap="word")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.insert("1.0", log)
        txt.config(state="disabled")
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _open_output(self):
        d = self.root_dir.get()
        if not d or not os.path.isdir(d):
            messagebox.showwarning(APP_TITLE, "Chưa có thư mục output để mở.")
            return
        try:
            os.startfile(d)  # Windows
        except Exception:  # noqa: BLE001
            subprocess.Popen(["explorer", d])

    def _check_ffmpeg_on_start(self):
        ok, info = core.check_ffmpeg()
        if not ok:
            messagebox.showwarning(
                APP_TITLE,
                "Không tìm thấy ffmpeg trên máy.\n\n"
                "Vui lòng cài đặt ffmpeg và thêm vào PATH:\n"
                "  • Tải tại https://www.gyan.dev/ffmpeg/builds/\n"
                "  • Giải nén và thêm thư mục bin vào biến môi trường PATH\n\n"
                "Tool vẫn mở nhưng sẽ không render được cho tới khi cài ffmpeg.")

    def _on_close(self):
        if self.is_rendering:
            if not messagebox.askyesno(
                    APP_TITLE, "Đang render. Bạn có chắc muốn thoát và dừng lại?"):
                return
            self.cancel_event.set()
            self._on_cancel()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
