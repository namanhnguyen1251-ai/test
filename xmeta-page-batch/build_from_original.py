"""Add the Page Batch options screen to the user's original XMETA extension ZIP.

The original extension stays on the user's machine; this script never downloads
or uploads it. Python 3.10+ and the standard library are sufficient.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile


ORIGINAL_SHA256 = "d4c6b24686da16eb8e40e501bd38bad52db47c23eb7984278bf9b774aa64b2aa"
PACKAGE_NAME = "XMETA_PageBatch_1.9.9"
ADDITIONS = ("page-batch.html", "page-batch.css", "page-batch.js")


def build(original_zip: Path) -> tuple[Path, Path]:
    script_dir = Path(__file__).resolve().parent
    original_zip = original_zip.resolve()
    if not original_zip.is_file():
        raise ValueError(f"Không tìm thấy ZIP gốc: {original_zip}")
    digest = hashlib.sha256(original_zip.read_bytes()).hexdigest()
    if digest != ORIGINAL_SHA256:
        raise ValueError("SHA-256 không khớp với ToolFB_1.9.9.zip đã phân tích; dừng để tránh vá nhầm bản.")

    for name in ADDITIONS:
        if not (script_dir / name).is_file():
            raise ValueError(f"Thiếu tệp bổ sung: {name}")

    destination = script_dir / "dist" / PACKAGE_NAME
    package_zip = script_dir / "dist" / f"{PACKAGE_NAME}.zip"
    if destination.exists() or package_zip.exists():
        raise ValueError("Thư mục hoặc ZIP trong dist đã tồn tại; hãy đổi tên bản cũ trước khi chạy lại.")

    with ZipFile(original_zip) as archive:
        entries = archive.infolist()
        if len(entries) > 100 or sum(item.file_size for item in entries) > 10_000_000:
            raise ValueError("ZIP gốc có kích thước hoặc số mục không phù hợp.")
        safe_entries = []
        for item in entries:
            name = item.filename
            parts = PurePosixPath(name).parts
            mode = item.external_attr >> 16
            if (
                not name or name.startswith(("/", "\\")) or "\\" in name
                or ".." in parts or ":" in name
                or stat.S_IFMT(mode) == stat.S_IFLNK
            ):
                raise ValueError(f"Mục ZIP không an toàn: {name}")
            safe_entries.append((item, Path(*parts)))

        destination.mkdir(parents=True)
        for item, relative in safe_entries:
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))

    manifest_file = destination / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 3 or manifest.get("version") != "1.9.9":
        raise ValueError("Manifest gốc không phải XMETA 1.9.9 Manifest V3.")
    manifest["options_ui"] = {"page": "page-batch.html", "open_in_tab": True}
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for name in ADDITIONS:
        (destination / name).write_bytes((script_dir / name).read_bytes())
    (destination / "README-page-batch.md").write_bytes((script_dir / "README.md").read_bytes())

    with ZipFile(package_zip, "w", compression=ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, f"{PACKAGE_NAME}/{path.relative_to(destination).as_posix()}")
    return destination, package_zip


if __name__ == "__main__":
    try:
        source = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(__file__).resolve().parent / "ToolFB_1.9.9.zip"
        folder, archive = build(source)
        print(f"Đã tạo thư mục tiện ích: {folder}")
        print(f"Đã tạo ZIP: {archive}")
        print("Chrome: chrome://extensions → Chế độ dành cho nhà phát triển → Tải tiện ích đã giải nén → chọn thư mục trên.")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        raise SystemExit(1)
