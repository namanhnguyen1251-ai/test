# Phân tích ToolFB_1.9.9 và bản Page Batch

## Phạm vi kiểm tra

Đầu vào là `ToolFB_1.9.9.zip` do người dùng cung cấp. Đây là tiện ích Chrome Manifest V3 tên XMETA.VN, phiên bản 1.9.9. Mã nguồn trong ZIP là JavaScript đọc được, không có tệp EXE. SHA-256 của ZIP gốc: `d4c6b24686da16eb8e40e501bd38bad52db47c23eb7984278bf9b774aa64b2aa`.

Tôi đối chiếu các tệp với luồng phân tích ứng dụng JavaScript trong [REA](https://github.com/morluto/rea/blob/main/docs/javascript-application-workflows.md): liệt kê các mô-đun, lần theo điểm vào, API, giao diện và đánh dấu phần không có trong artifact. REA CLI không được thực thi vì `rea-agents` không có trong npm cache của môi trường và tải npm bị hạn chế. Những kết luận dưới đây đến từ mã ZIP và kiểm tra tĩnh trực tiếp.

## Cấu trúc và phụ thuộc

- `manifest.json` khai báo service worker `js/background.js`; `js/content.js` và `js/login.js` chạy trên mọi trang. Tiện ích yêu cầu quyền cookie, proxy, storage, tabs và host `<all_urls>`.
- Khi bấm biểu tượng, `background.js` mở `apiBase` mặc định `https://xmeta.vn`. `content.js` nhúng giao diện `/popup` và `/bm-popup.html` từ cùng máy chủ. Giao diện web và backend đó không nằm trong ZIP.
- `background.js` đọc `/api/tenant-status` và `/api/ext-shield` để nhận địa chỉ và cấu hình động. Do đó không thể dựng lại toàn bộ chức năng XMETA ngoại tuyến chỉ từ ZIP.
- Không tìm thấy mã tạo Facebook Page mới trong ZIP. Đoạn Page có sẵn ở cuối `background.js` xử lý trả lời bình luận cho Page đã tồn tại.

## Phần được thêm

Bản `XMETA_PageBatch_1.9.9.zip` giữ nguyên toàn bộ mã/tài nguyên gốc, ngoài `manifest.json` thêm `options_ui`. Ba tệp `page-batch.html`, `page-batch.css`, `page-batch.js` cung cấp màn hình CSV, xem trước, tạo Page tuần tự, tạm dừng, khôi phục tiến trình và xuất kết quả. Token chỉ nằm trong bộ nhớ trang. Khi phản hồi không xác định được Page ID hoặc lỗi máy chủ, công cụ dừng và đánh dấu **Chưa rõ** để tránh tạo trùng khi chạy lại.

Màn hình mới dùng Meta Graph API `GET /me?fields=id`, sau đó `POST /{user-id}/accounts`, như mô tả trong [Meta Business SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/user.py#L2700-L2763). Theo [Meta User Accounts reference](https://developers.facebook.com/docs/graph-api/reference/user/accounts/), thao tác tạo Page chỉ dành cho tài khoản thử nghiệm hoặc app được Meta cho phép và cần quyền phù hợp. Không có cách bảo đảm tạo số lượng lớn mà không gặp giới hạn hay kiểm duyệt spam của Meta.

## Kiểm tra

- Toàn bộ JavaScript qua `node --check`; `manifest.json` hợp lệ; ZIP mới qua `unzip -t`.
- Test DOM/fetch giả lập xác nhận đường POST, dừng trên lỗi quyền/lạm dụng, đánh dấu phản hồi mơ hồ và không lưu token.
- Chưa thử tạo Page thật vì không có token/app Meta được cấp quyền. Chromium trong môi trường này cũng lỗi quyền hệ thống trước khi tải tiện ích, ngay cả với hồ sơ trống.

SHA-256 gói mới: `3f03f3fb54c848dfbf68c32f22338dec9c98a44028a209312803e7651f75e4e6`.
