# XMETA.VN 1.9.9 + Page Batch

Thư mục này thêm màn hình tạo Facebook Page theo lô vào **bản ZIP gốc do bạn sở hữu**. Mã đăng nhập, cookie và 2FA của tiện ích gốc không được đưa lên GitHub. Script ghép chạy cục bộ, giữ các tệp gốc và thêm trang **Tùy chọn tiện ích**; nút XMETA.VN trên thanh công cụ vẫn mở trang như trước.

## Tạo bản tiện ích chạy được

1. Tải thư mục này từ kho GitHub. Đặt `ToolFB_1.9.9.zip` gốc cạnh `build_from_original.py`, hoặc nhớ đường dẫn đến ZIP gốc.
2. Cài Python 3.10 trở lên. Trong thư mục này chạy:

   ```bash
   python build_from_original.py ToolFB_1.9.9.zip
   ```

   Trên Windows, có thể dùng `py -3 build_from_original.py ToolFB_1.9.9.zip`.
3. Mở `chrome://extensions`, bật **Chế độ dành cho nhà phát triển**, chọn **Tải tiện ích đã giải nén**, rồi chọn `dist/XMETA_PageBatch_1.9.9`.
4. Mở **Chi tiết → Tùy chọn tiện ích** để vào màn hình tạo Page. Bản ZIP để lưu/di chuyển cũng nằm trong `dist`.

Script kiểm tra SHA-256 của ZIP gốc để tránh vá nhầm một bản khác. Nếu chạy lại, hãy đổi tên thư mục và ZIP cũ trong `dist` trước; script không tự xóa dữ liệu.

## Tạo Page theo lô

CSV UTF-8 cần `name` và `category_enum`; `about` có thể bỏ trống. `category_enum` là mã `api_enum` của [Meta Page Category](https://developers.facebook.com/docs/graph-api/reference/page-category/), không phải tên danh mục dịch ra tiếng Việt.

```csv
name,category_enum,about
Trang Mẫu A,PERSONAL_BLOG,Giới thiệu Trang Mẫu A
Trang Mẫu B,PERSONAL_BLOG,Giới thiệu Trang Mẫu B
```

Công cụ dùng Meta Graph API để kiểm tra User token rồi tạo từng Page, hiển thị trạng thái, cho tạm dừng và xuất CSV kết quả. Token chỉ ở bộ nhớ trang và bị xóa khi dừng/hoàn tất. Danh sách và trạng thái được lưu cục bộ để khôi phục; yêu cầu có kết quả mơ hồ được đánh dấu **Chưa rõ** và cần kiểm tra trên Meta trước khi thử lại.

Meta giới hạn API tạo Page cho tài khoản thử nghiệm hoặc app được Meta cho phép; token cần quyền `pages_manage_metadata` và `pages_show_list`. Nếu chưa có quyền này, công cụ không thể tạo Page thật. Công cụ tuân theo giới hạn API, dừng khi Meta từ chối hoặc cảnh báo lạm dụng; không có tốc độ nào bảo đảm tránh bộ lọc spam. Xem [Meta User Accounts](https://developers.facebook.com/docs/graph-api/reference/user/accounts/) và [Meta Business SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/user.py#L2700-L2763).

ZIP gốc không chứa giao diện web và máy chủ XMETA.VN. Các chức năng gốc phụ thuộc `xmeta.vn` vẫn cần dịch vụ đó. Xem tài liệu phân tích đi kèm trong kho để biết ranh giới này.
