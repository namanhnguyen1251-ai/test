# PagePilot — Đăng bài hàng loạt lên Facebook Fanpage

Ứng dụng chạy trên máy tính của bạn, với giao diện tiếng Việt mở trong trình duyệt. PagePilot sử dụng Meta Graph API và Meta App hiện có của bạn để lấy các Fanpage mà User Access Token được phép quản lý, rồi đăng nội dung lên những Page bạn chọn.

## Chức năng

- Lấy danh sách Page qua `/me/accounts`, đọc tiếp các trang kết quả để không bỏ sót danh sách khoảng 50 Fanpage.
- Tìm kiếm, chọn một hoặc nhiều Page, xem lại nội dung và danh sách đích trước khi đăng.
- Đăng văn bản; bài có ảnh JPEG/PNG; video MP4; Facebook Reels từ file MP4 trên máy.
- Chuẩn bị nhiều bài riêng biệt và gửi lịch đăng lên Meta; Meta xử lý lịch đã nhận dù máy tính của bạn tắt.
- Tùy chọn bình luận dưới bài bằng chính Fanpage sau khi xác nhận bài đã xuất bản; tính năng bình luận cần ứng dụng đang chạy.
- Xử lý lần lượt, theo dõi kết quả riêng cho từng Page và dừng các mục chưa bắt đầu.
- Chế độ demo có 50 Page giả để thử giao diện, không gửi bài lên Meta.
- Lưu lịch sử công việc trên máy; token chỉ nằm trong bộ nhớ trong thời gian ứng dụng chạy.

Số Page thực tế phụ thuộc quyền token và việc cấp quyền Page cho người dùng trong Business Portfolio. Người dùng quản lý 50 Page không đồng nghĩa token tự động được truy cập đủ 50 Page.

## Chạy trên Windows

1. Cài **Python 3.10 trở lên** từ [python.org](https://www.python.org/downloads/windows/), kèm `pip` và **Python Launcher** để dùng lệnh `py`. Nếu dùng bộ cài có lựa chọn này, bật **Add Python to PATH**.
2. Giải nén mã nguồn vào một thư mục trên máy.
3. Nhấp đúp `start_windows.bat`. Lần đầu script tạo `.venv` và cài các thư viện trong `requirements.txt`; cần kết nối Internet.
4. Ứng dụng mở trong trình duyệt. Nếu trình duyệt không mở tự động, xem địa chỉ cục bộ trong cửa sổ lệnh rồi nhập vào trình duyệt trên máy bạn.
5. Giữ cửa sổ lệnh mở trong lúc sử dụng. Nhấn `Ctrl+C` trong cửa sổ đó để thoát.

Hoặc chạy thủ công trong PowerShell, tại thư mục mã nguồn:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Không cần bật PowerShell execution policy vì các lệnh trên gọi trực tiếp Python trong môi trường ảo. Đây là ứng dụng Python chạy cục bộ, chưa phải bộ cài `.exe` độc lập.

Nếu cổng 8765 đang được ứng dụng khác sử dụng, chạy `.\.venv\Scripts\python.exe app.py --port 8766`. Có thể dùng `--no-browser` để không tự mở trình duyệt hoặc `--data-dir` để chỉ định thư mục lịch sử.

Trên macOS/Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Các thư viện được ghim trong `requirements.txt`: `requests==2.32.5` và `requests-toolbelt==1.0.0`. Video được gửi theo luồng để tránh đọc toàn bộ video lớn vào RAM.

### Cập nhật khi gặp lỗi URL tải Reel

Bản cập nhật sửa lỗi kiểm tra URL tải Reel quá chặt: phiên bản dịch vụ upload có thể khác phiên bản Graph API gửi lệnh start. Tool giữ nguyên tham số URL do Meta trả về và chỉ tải lên HTTPS của `rupload.facebook.com`, với đường dẫn khớp mã video. URL tải không xuất hiện trong giao diện hoặc lịch sử.

Đóng tool bằng `Ctrl+C`, tải gói ZIP mới từ nhánh `pagepilot-download`, giải nén vào thư mục mới rồi chạy `start_windows.bat`. Kết nối lại bằng token và phiên bản Graph API bạn đang dùng; thử một Fanpage trước khi chạy hàng loạt. Những mục gặp đúng lỗi kiểm tra URL trên đã bị chặn trước bước upload và finish.

## Kết nối Meta App của bạn

Ứng dụng dùng **User Access Token từ Meta App của bạn** để lấy Page Access Token tương ứng. Bản này chưa tích hợp luồng Facebook Login; bạn nhập token đã có vào giao diện cục bộ. Không nhập App Secret vào ô token và không gửi token trong chat, issue hoặc ảnh chụp màn hình.

Nhập **Graph API version** được Meta App của bạn hỗ trợ, theo dạng `vNN.0`. Trường này bắt buộc; ứng dụng không tự đoán phiên bản mới nhất. Kiểm tra phiên bản trong App Dashboard và tài liệu Meta trước khi dùng.

Những quyền cần kiểm tra cho quy trình đăng Page:

- `pages_show_list`: lấy danh sách Page mà người dùng đã cấp quyền cho app.
- `pages_read_engagement`: đọc dữ liệu Page cần cho các thao tác Page API.
- `pages_manage_posts`: tạo bài đăng trên Page.
- `pages_manage_engagement`: tạo bình luận bằng Fanpage, nếu bật tính năng bình luận sau đăng.

Người dùng phải có quyền tạo nội dung tương ứng trên từng Page trong Facebook/Business Portfolio. Kiểm tra cả quyền được cấp cho người dùng, Page được chọn khi cấp quyền app và các task mà `/me/accounts` trả về, chẳng hạn `CREATE_CONTENT` hoặc quyền quản lý phù hợp theo mô hình Page hiện tại. Chỉ có tên Page xuất hiện trong một danh sách quản lý Business chưa đủ để chứng minh được phép đăng.

Chế độ Development, vai trò app và mức truy cập quyền có thể giới hạn những tài khoản được sử dụng. Khi cung cấp app cho người dùng ngoài nhóm có vai trò trong app, Meta có thể yêu cầu Live mode, Advanced Access/App Review và Business Verification, tùy loại app, use case và quyền được xin. Xem yêu cầu thực tế trong App Dashboard; tool không tự cấp quyền và không thay thế quy trình xét duyệt.

Token có thể hết hạn hoặc bị thu hồi. Khi gặp lỗi quyền hoặc token, cấp lại token trong app của bạn rồi kết nối lại. Nếu app bật yêu cầu `appsecret_proof`, cần kiểm tra khả năng tương thích riêng; đừng tắt một yêu cầu bảo mật của app chỉ để thử đăng bài.

## Cách dùng

1. Có thể thử chế độ demo trước để làm quen với danh sách 50 Page và quy trình xác nhận.
2. Kết nối bằng token thật và Graph version hợp lệ; chờ tải xong danh sách Page.
3. Chọn các Page cần đăng, chọn loại bài, nhập nội dung và chọn file nếu cần.
4. Kiểm tra lại bản xem trước và danh sách Page trong bước xác nhận, rồi bắt đầu.
5. Theo dõi từng kết quả. Với video/Reels, Meta xử lý bất đồng bộ; nhận được video ID hoặc phản hồi nhận yêu cầu chưa đồng nghĩa video đã hiển thị thành công.

### Hẹn giờ khi máy tính tắt

Soạn từng bài rồi thêm vào danh sách kế hoạch. Mỗi bài giữ nội dung, tệp đính kèm, danh sách Page, thời gian và tùy chọn bình luận riêng; sửa bộ soạn sau đó không thay đổi những bài đã thêm. Kiểm tra toàn bộ kế hoạch trong bước xác nhận, rồi gửi lịch. PagePilot tải nội dung lên Meta ngay khi bạn gửi; thời gian hẹn là lúc Meta xuất bản bài. Bạn vẫn có thể chọn đăng ngay.

**Chỉ tắt máy sau khi quá trình tải lên và gửi lịch của tất cả bài/Page đã hoàn tất.** Bài có trạng thái đã được Meta nhận lịch được Meta xử lý về sau, không cần PagePilot tiếp tục chạy. Các mục còn chờ trên máy, lỗi hoặc chưa rõ kết quả chưa có bảo đảm này. Kiểm tra lịch trong Meta Business Suite, nhất là lần đầu dùng.

Lịch đăng được gửi qua tham số lập lịch chính thức: văn bản và bài nhiều ảnh dùng `published=false` cùng `scheduled_publish_time`; video thường dùng cùng các tham số đó; Reels dùng `video_state=SCHEDULED` cùng `scheduled_publish_time` ở bước hoàn tất. Việc nhận lịch không cam kết bài sẽ xuất bản thành công nếu Meta phát hiện lỗi media, quyền hoặc nội dung về sau.

Giao diện hiển thị theo múi giờ máy/trình duyệt; máy đặt Bangkok dùng UTC+7. Đọc kỹ múi giờ và thời gian trước khi xác nhận. Thời gian gửi lên Meta là UNIX timestamp nên giữ đúng thời điểm qua các múi giờ.

Tool giới hạn lịch thật trong khoảng **20 phút đến 28 ngày** tính từ lúc gửi, để có khoảng đệm tải nội dung lên nhiều Page. Đây là chính sách của PagePilot, không phải tuyên bố giới hạn chung của mọi API Meta. Việc chấp nhận lịch còn phụ thuộc API version, loại bài và quy định hiện hành của Meta. Nếu Meta từ chối lịch, tool giữ kết quả lỗi để bạn kiểm tra; không đổi sang đăng ngay. Demo cho phép lịch từ 5 giây để thử quy trình và không gọi Meta.

### Bình luận bằng Fanpage

Bình luận dùng Page Access Token nên tác giả là chính Fanpage, không phải tài khoản cá nhân. Công cụ chỉ bình luận sau khi kiểm tra bài đã xuất bản; thời gian hẹn đến hoặc video có ID chưa đủ để xác nhận điều này.

Bạn có thể đặt khoảng chờ bình luận từ **0 đến 3.600 giây**. Khoảng chờ tính từ khi PagePilot quan sát được xác nhận bài đã xuất bản, không tính từ lúc gửi lịch. Tool kiểm tra trạng thái khoảng 5 giây/lượt; mạng, thời gian xử lý video và số bài đang theo dõi có thể làm bình luận xuất hiện muộn hơn.

Meta không nhận một lịch bình luận riêng trong quy trình này. Vì vậy, **tự bình luận cần PagePilot đang chạy và có token hợp lệ**. Nếu tắt máy, bài đã được Meta nhận lịch vẫn có thể xuất bản, còn bình luận chờ bạn mở lại ứng dụng và kết nối lại đúng tài khoản. Thiếu token chỉ tạm dừng theo dõi/bình luận trên máy, không ngăn Meta xuất bản lịch đã nhận. Nội dung bình luận, Page ID và post/video ID được lưu cục bộ; token vẫn chỉ giữ trong RAM.

Lỗi bình luận được hiển thị riêng theo Page. Tool không tự gửi lại bình luận lỗi hoặc chưa rõ kết quả, nhằm tránh tạo bình luận trùng; việc đọc trạng thái có thể được kiểm tra lại.

### Tệp đính kèm và hàng đợi

Giới hạn file **do tool đặt**: tối đa 10 ảnh JPEG/PNG, mỗi ảnh không quá 20 MB; một video MP4 không quá 1 GB. Đây không phải cam kết về giới hạn hoặc yêu cầu media hiện hành của Meta. Meta vẫn kiểm tra định dạng, thời lượng, tỷ lệ khung hình, quyền và nội dung. Cần dùng media đáp ứng yêu cầu trong tài liệu Meta cho API version của app.

Nút dừng ngăn các mục tiếp theo bắt đầu; yêu cầu đang gửi có thể vẫn hoàn tất. Không thể ngắt kết nối token khi công việc còn chạy. Giữ ứng dụng và mạng hoạt động cho tới khi hàng đợi gửi bài/gửi lịch kết thúc. Dừng hàng đợi hoặc thoát ứng dụng không hủy những lịch Meta đã nhận; kiểm tra và quản lý các lịch đó trong Meta Business Suite. Sau khi khởi động lại, các mục chưa được gửi trước lúc thoát không được tự động gửi; các lịch đã có ID Meta vẫn được giữ để theo dõi.

Ứng dụng không tự thử lại các yêu cầu đăng, vì lỗi mạng sau khi gửi có thể xảy ra dù Meta đã nhận bài. Nếu kết quả không rõ, kiểm tra trực tiếp trên Page và bằng ID trả về trước khi chọn đăng lại; việc gửi lại có thể tạo bài trùng. Nếu tải nhiều ảnh thành công nhưng tạo bài cuối cùng thất bại, ảnh chưa xuất bản có thể còn trên Meta.

## Dữ liệu cục bộ

Máy chủ chỉ lắng nghe trên loopback, để trình duyệt trên cùng máy kết nối. Token người dùng và token Page giữ trong RAM, không ghi vào cơ sở dữ liệu hay lịch sử; được xóa khi ngắt kết nối hoặc thoát máy chủ. Đây vẫn là thông tin nhạy cảm trong phiên đang chạy, nên chỉ nhập vào ứng dụng cục bộ trên máy bạn kiểm soát.

Trên Windows, lịch sử được lưu tại `%LOCALAPPDATA%\PagePilot\history.sqlite3`; trên macOS/Linux, mặc định là `~/.local/share/PagePilot/history.sqlite3` hoặc thư mục theo `XDG_DATA_HOME`. Ứng dụng in đường dẫn đang dùng khi khởi động. Lịch sử chứa nội dung và kết quả công việc, vì vậy vẫn có thể chứa thông tin riêng của bạn dù không chứa token. Có thể xuất kết quả từng công việc ra CSV trong giao diện. Media được lưu tạm phục vụ phiên tải lên, không được dùng làm kho media lâu dài.

Kết nối mạng cần thiết: `graph.facebook.com` để gọi Graph API và kiểm tra trạng thái; `graph-video.facebook.com` để tải video thường; `rupload.facebook.com` để tải Reels; `pypi.org` và `files.pythonhosted.org` khi cài thư viện. Kết nối HTTPS giữ xác minh chứng chỉ TLS.

## Kiểm thử và phạm vi

Trên Windows:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Trên macOS/Linux:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Các kiểm thử sử dụng phản hồi API giả lập, không đăng bài thật lên Meta. Luồng Graph API được đối chiếu với mã mẫu/SDK chính thức của Meta; chưa được xác nhận bằng User Access Token và Page thật của bạn trong môi trường này. Hãy kiểm tra một Page với nội dung do bạn chọn trước khi áp dụng cho toàn bộ danh sách.

Bản này phục vụ Facebook Pages: văn bản, ảnh, video và Reels, với đăng ngay hoặc gửi lịch lên Meta. Bình luận là của Fanpage và cần ứng dụng hoạt động; chưa có máy chủ riêng chạy bình luận 24/7. Chưa tích hợp Instagram, tài khoản cá nhân, Facebook Groups, tự đăng nhập OAuth hoặc đóng gói bộ cài Windows. Không cần App Secret để chạy luồng nhập token hiện tại.

## Tài liệu Meta tham khảo

- [Pages API — Getting Started](https://developers.facebook.com/docs/pages-api/getting-started/)
- [Pages API — Posts](https://developers.facebook.com/docs/pages-api/posts/)
- [Page Feed](https://developers.facebook.com/docs/graph-api/reference/page/feed/)
- [Page Photos](https://developers.facebook.com/docs/graph-api/reference/page/photos/)
- [Video publishing](https://developers.facebook.com/docs/video-api/guides/publishing/)
- [Facebook Reels publishing](https://developers.facebook.com/docs/video-api/guides/reels-publishing/)
- [Mã mẫu Reels chính thức của Meta](https://github.com/fbsamples/reels_publishing_apis)
- [Facebook Python Business SDK chính thức](https://github.com/facebook/facebook-python-business-sdk)

Nguồn đã đọc qua Git trong lần triển khai này: mẫu Reels tại commit `7bf98f94e75d841ecc9612c0061881d1d713c6bd`, SDK tại commit `efd8423a2e595ea8d4c04eb824ce113f2f1d68cd`. Host tải video thường được đối chiếu với `VideoUploadRequest.send` trong [video_uploader.py của SDK](https://github.com/facebook/facebook-python-business-sdk/blob/efd8423a2e595ea8d4c04eb824ce113f2f1d68cd/facebook_business/video_uploader.py); đây là đối chiếu mã nguồn, chưa phải kiểm chứng upload Page trực tiếp. Trang tài liệu `developers.facebook.com` không truy cập được từ môi trường phát triển tại thời điểm kiểm tra; do đó chưa xác minh được phiên bản Graph mới nhất hay giới hạn media mới nhất. Các phiên bản API cũ trong mã mẫu chỉ là ví dụ, không phải phiên bản mặc định cho tool.
