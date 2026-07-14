# Công cụ Render Video Hàng Loạt (Auto Batch Video Render Tool)

Ứng dụng desktop có giao diện, tự động ghép **ảnh + audio + phụ đề .srt** trong nhiều
thư mục con thành video, xử lý **song song** với các hiệu ứng: tuyết rơi, **rung máy nhẹ
(mượt, kiểu quay tay)**, phụ đề phát sáng — kèm chỉnh **vị trí dọc phụ đề**.

Toàn bộ pipeline ffmpeg được giữ nguyên theo bản CLI đã test thực tế.

## Cài đặt nhanh (cho người mới)

1. **Cài Python** (bản mới): https://www.python.org/downloads/ — khi cài nhớ **tích ô
   "Add Python to PATH"**.
2. **Cài ffmpeg** và thêm vào PATH: https://www.gyan.dev/ffmpeg/builds/ → tải bản
   `full`, giải nén, thêm thư mục `bin` vào biến môi trường PATH.
   (Kiểm tra: mở CMD gõ `ffmpeg -version` thấy thông tin là được.)
3. **Tải tool này về:** bấm nút xanh **Code → Download ZIP** trên trang GitHub, rồi giải nén;
   hoặc `git clone <link-repo>`.
4. Vào thư mục vừa giải nén, **nhấp đúp `tao_shortcut.bat`** để tạo icon ngoài Desktop.
5. Từ nay mở tool bằng **icon "Render Video Hang Loat"** trên Desktop (hoặc nhấp đúp `run.bat`).

## Yêu cầu

- **Python 3.9+** (đã test trên 3.14) — dùng `tkinter` có sẵn, **không cần cài thêm thư viện**.
- **ffmpeg** đã cài và thêm vào `PATH`. Kiểm tra: mở terminal gõ `ffmpeg -version`.
  - Tải: https://www.gyan.dev/ffmpeg/builds/ → giải nén → thêm thư mục `bin` vào PATH.

## Chạy

- Nhấp đúp **`run.bat`** (không hiện cửa sổ console), hoặc
- Chạy trong terminal: `python app.py`

## Chuẩn bị dữ liệu

```
thư-mục-gốc/
├── snow.mp4              (video tuyết — có thể chọn qua UI)
├── project-01/
│   ├── anh.jpg           (.jpg .jpeg .png .webp)
│   ├── loi-thoai.mp3     (.mp3 .wav .m4a .aac)
│   └── phu-de.srt        (.srt)
├── project-02/
│   └── ...
```

- Tool **tự nhận diện file theo phần mở rộng**, không phụ thuộc tên file.
- Project nào thiếu file sẽ bị **bỏ qua kèm cảnh báo**, không làm dừng cả loạt.
- Video xuất ra tên `output.mp4` **ngay trong từng thư mục project**.

## Các bước dùng

1. **Chọn thư mục gốc** → tool liệt kê số project tìm thấy, đánh dấu project sẵn sàng.
2. Bật/tắt **hiệu ứng**, chỉnh **phụ đề** (font, cỡ, màu chữ, màu viền, độ dày, in đậm).
3. Chọn **độ phân giải** (480p/720p/1080p/4K), FPS, CRF, số luồng song song.
4. **👁 Xem thử 1 video** để kiểm tra hiệu ứng trước khi chạy hàng loạt.
5. **▶ Render tất cả** → theo dõi tiến trình từng project; nhấn **■ Dừng** để hủy.
6. Nhấp đúp một dòng bị **❌ Lỗi** để xem log ffmpeg chi tiết.
7. **📂 Mở thư mục output** khi xong.

## Cấu trúc mã

| File | Vai trò |
|------|---------|
| `render_core.py` | Lõi ffmpeg (dựng filter, convert màu hex→ASS, chạy lệnh). Thuần, dễ test. |
| `app.py` | Giao diện Tkinter + điều phối render song song có tiến trình. |
| `run.bat` | Khởi động nhanh bằng `pythonw`. |

## Nền video phong cảnh (ảnh chính đè giữa)

- Section **"🎞️ Nền video phong cảnh"**: dùng các **video phong cảnh làm nền** (giữ SẮC NÉT,
  không làm mờ) phủ kín khung, **ảnh chính đè ở giữa** (~62%, chỉnh bằng slider "Cỡ ảnh chính"),
  phụ đề bên dưới. (Muốn làm mờ nền thì đặt `scenery_blur` > 0 trong cấu hình — mặc định 0.)
- Bấm **"Chọn..."** trỏ tới **1 thư mục chứa các video phong cảnh** (mp4/mov/mkv...). Dùng
  chung cho mọi project.
- **Tự xáo trộn + lặp:** dù video phong cảnh chỉ vài giây tới ~30s, tool tự **xáo trộn ngẫu
  nhiên và ghép** cho **vừa đúng độ dài voice** (kể cả voice 1 tiếng). Mỗi project một thứ tự
  ngẫu nhiên khác nhau.
- Video phong cảnh khác độ phân giải/fps đều được **chuẩn hóa 1 lần và cache lại** (thư mục
  `%TEMP%/vbt_scenery_cache`) — các project sau dùng lại ngay, không encode lại.
- Ở chế độ này ảnh chính **đứng yên** (không áp hiệu ứng lắc). Tuyết/sóng âm vẫn chồng được lên trên.
- **Tăng tốc GPU cho nền phong cảnh:** khi chọn encoder **NVENC (NVIDIA)**, tool tự động
  **giải mã video nền + ghép ảnh chính trên GPU** (NVDEC + `overlay_cuda`) → giảm mạnh tải CPU
  (chỉ còn phụ đề chạy CPU vì libass không có bản GPU). Nếu GPU lỗi, tool **tự chuyển về CPU**
  để không hỏng render. Encoder AMD/Intel/CPU thì giải mã GPU qua `-hwaccel auto`, ghép trên CPU.

## Hiệu ứng sóng âm thanh (overlay)

- Ô **"🎵 Sóng âm thanh (overlay)"** trong panel Hiệu ứng: chồng một **video trực quan
  sóng âm** (dạng dải sáng/spectrum trên **nền đen**) lên video — giống hệt cơ chế tuyết.
- Bấm **"Chọn..."** trỏ tới file video sóng âm của bạn; **slider "Độ đậm sóng"** chỉnh độ mờ.
- Dùng blend `screen` trong RGB nên **nền đen của file tự biến mất**, chỉ còn dải sóng phát
  sáng hiện lên. File cần là **sóng sáng trên nền đen** (đa số template music-visualizer đều vậy).
- Lưu ý: đây là overlay trang trí lặp lại theo video, **không phản ứng theo đúng âm thanh**
  của từng project (giống tuyết). Có thể bật cùng lúc với tuyết.

## Hiệu ứng lắc nhẹ sang 2 bên (mượt)

- Thay cho "Rung máy quay" (xoay tròn máy móc) + "Motion zoom" (zoompan hay giật) trước đây.
- Quỹ đạo **parabol (vòng cung)** cho mượt & tự nhiên: ngang là sóng sin chậm (chu kỳ 8s),
  dọc dao động ở **tần số gấp đôi** (y ~ x²) → khi ảnh trôi trái → giữa → phải nó vẽ một
  **cung cong** (thấp giữa, cao 2 mép), thay vì trượt thẳng ngang. Đổi chiều rất êm
  (vận tốc = 0 ở 2 đầu), **không sóng tần số cao nên không rung/giật**.
- Đã đo thực: ngang ±54px, dọc ±23px đúng dạng parabol (thấp ở giữa, cao ở 2 biên).
- **Chống giật (supersampling):** `crop` của ffmpeg chỉ dịch được nguyên pixel nên lắc
  chậm sẽ nhảy 0,1,0,1 px (giật lăn tăn). Tool phóng nền lên F× rồi crop-pan, sau đó thu nhỏ
  lại → vị trí lẻ pixel được nội suy → trôi đều ~0.5px/khung (đã đo). Đổi lại khâu lọc chậm hơn.
- **Ô "Độ mượt"** (dưới slider độ rộng lắc) chọn mức supersampling:
  - **Thường (nhanh)** = 2× — mượt, nhanh; mặc định, hợp render hàng loạt.
  - **Cao (mượt hơn, chậm)** = 3× — mịn hơn cho ảnh nhiều chi tiết, chậm hơn ~2×.
  - Tắt hẳn "Lắc" nếu cần render nhanh tối đa (encode vẫn trên GPU).
- Slider **"Độ rộng lắc (px)"** chỉnh biên độ trôi (mặc định 20). Biên độ luôn nhỏ hơn lề crop
  nên **không lộ mép đen**; sau crop tự scale lại đúng resolution.

## Vị trí phụ đề

- Ô **"Vị trí (lề dưới)"** trong panel Phụ đề: số **nhỏ = sát đáy**, **lớn = nhấc cao hơn**.
- Mặc định **10** đặt phụ đề thấp, căn giữa, nhấc nhẹ khỏi đáy (khớp bố cục video mẫu).
- Giá trị theo thang ASS (PlayResY ~288) nên **tự co giãn theo độ phân giải** — cùng một số
  cho vị trí tương đối như nhau ở 480p/720p/1080p/4K.

## Tăng tốc GPU (encode)

- Khi mở app, tool **tự dò** encoder GPU khả dụng (encode thử 1 clip nhỏ) và hiện ở
  ô **"Bộ mã hóa"**. Nếu có GPU chạy được, app **tự chọn GPU** (nhãn "✅ Đã bật GPU").
- Hỗ trợ: **NVIDIA NVENC**, **AMD AMF**, **Intel QSV**. Chỉ những cái thực sự chạy
  được trên máy mới hiện ra; luôn có sẵn **CPU (x264)** để dự phòng.
- **Lưu ý NVIDIA:** ffmpeg 8.0 yêu cầu **driver NVIDIA ≥ 570**. Nếu driver cũ, NVENC
  sẽ không hiện — hãy cập nhật driver GeForce rồi mở lại app.
- Các hiệu ứng (scale/zoom/rung/tuyết/phụ đề) vẫn xử lý trên CPU; chỉ khâu **nén video**
  chạy trên GPU — nên toàn bộ logic đã test được giữ nguyên, chỉ nhanh hơn ở bước xuất.
  ffmpeg **không có** phiên bản GPU cho zoompan/blend/subtitles, nên CPU luôn được dùng
  một phần cho hiệu ứng dù đã chọn NVENC — đây là giới hạn của ffmpeg, không phải lỗi tool.
- Thang **CRF (18–28)** dùng chung cho mọi encoder (GPU quy đổi sang CQ/QP tương ứng).

## Chế độ chạy: Lần lượt vs Song song

- **Lần lượt**: render từng folder một, xong cái này mới sang cái kia. CPU nhẹ nhất,
  máy còn tài nguyên làm việc khác — nhưng GPU hay phải chờ CPU nên **tổng thời gian lâu nhất**.
- **Song song [N] video/lần**: nhiều video xử lý cùng lúc → CPU lo hiệu ứng video này
  trong khi GPU nén video kia → **tổng thời gian nhanh hơn** cho cả loạt (VD 30 video).
- **Nên chọn:** *Song song* để tối ưu tốc độ. Điểm ngọt là
  **(số video song song) × (luồng CPU/video) ≈ số nhân CPU**. Máy 12 nhân → ví dụ
  3 video × 2 luồng = 6, hoặc 2 video × 3 luồng. Chọn *Lần lượt* khi cần máy nhẹ để làm việc khác.

## Khống chế mức ăn CPU

- **Số video song song**: bao nhiêu video render cùng lúc.
- **Luồng CPU/video**: giới hạn số luồng filter cho mỗi video (`-filter_complex_threads`).
- Dòng chữ **"ước tính chiếm CPU: ~X%"** cập nhật ngay khi bạn chỉnh 2 ô trên
  (Tổng luồng ≈ số video song song × luồng/video, so với số nhân CPU).
- **Mặc định TỰ ĐỘNG theo cấu hình máy** để render nhanh nhất (dùng gần hết CPU, ~100%):
  số video song song = tối đa 6, luồng/video chia phần còn lại. Máy càng mạnh chạy càng mạnh.
- Muốn **máy nhẹ để làm việc khác** → giảm 2 ô (VD Song song 2 × Luồng 2 ≈ 33%).
- **GPU thấp là bình thường:** khâu nén NVENC rất nhẹ; nút thắt là hiệu ứng chạy CPU nên
  GPU luôn "rảnh". Cứ nhìn **thời gian render**, không nhìn % GPU. Muốn GPU bận hơn +
  nhanh nhất tổng thể thì **render nhiều video song song** (nhiều phiên NVENC cùng lúc).

## Thanh tiến độ từng video

- Mỗi dòng có cột **"Tiến độ"** hiển thị thanh `████████░░░░ %` cập nhật real-time
  (đọc từ luồng `-progress` của ffmpeg, quy đổi theo độ dài audio).
- Thanh tổng phía dưới chạy **mượt theo tổng phần trăm** tất cả video, kèm nhãn
  "X/Y video hoàn thành".

## Ghi chú kỹ thuật (giữ nguyên theo spec)

- Scale lại sau bước camera-shake `crop` để tránh lỗi `Failed to configure output pad`.
- `-pix_fmt yuv420p` để phát được trên iPhone/QuickTime.
- Snow dùng `-stream_loop -1` + `-shortest`: tự lặp khớp đúng độ dài audio.
- **Blend tuyết chạy trong RGB (`format=gbrp`)**, KHÔNG để mặc định YUV. Nếu blend
  `screen` trên YUV, hai kênh chroma bị đẩy lệch → **ám hồng/tím toàn khung hình**.
  Chuyển sang RGB thì nền đen của snow.mp4 mới thực sự biến mất và màu gốc được giữ nguyên.
- Màu chữ: color picker trả hex `#RRGGBB` → tự convert sang ASS `&HAABBGGRR` (đảo BGR).
- Dùng `subprocess` (không `os.system`) để bắt stderr ffmpeg và hiển thị log lên UI.
