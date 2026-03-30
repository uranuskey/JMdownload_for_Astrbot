from pathlib import Path

import pyzipper
from PIL import Image


class PackageService:
    @staticmethod
    def images_to_pdf(
        image_files: list[Path],
        task_dir: Path,
        album_id: str,
        profile: str = "balanced",
        layout_mode: str = "multipage",
        long_page_max_images: int = 80,
        long_page_max_height: int = 60000,
    ) -> Path:
        if not image_files:
            raise RuntimeError("未找到可用于生成 PDF 的图片")

        profile = (profile or "balanced").strip().lower()
        if profile not in {"fast", "balanced", "high"}:
            profile = "balanced"

        layout_mode = (layout_mode or "multipage").strip().lower()
        if layout_mode not in {"multipage", "longpage"}:
            layout_mode = "multipage"

        if layout_mode == "longpage":
            return PackageService._images_to_single_long_pdf(
                image_files=image_files,
                task_dir=task_dir,
                album_id=album_id,
                profile=profile,
                long_page_max_images=max(1, int(long_page_max_images or 1)),
                long_page_max_height=max(1000, int(long_page_max_height or 1000)),
            )

        rgb_images: list[Image.Image] = []
        try:
            for image_path in image_files:
                with Image.open(image_path) as img:
                    normalized = PackageService._normalize_image_for_profile(img, profile)
                    rgb_images.append(normalized.copy())
                    normalized.close()

            pdf_path = task_dir / f"{album_id}.pdf"
            first, rest = rgb_images[0], rgb_images[1:]
            first.save(pdf_path, save_all=True, append_images=rest)
            return pdf_path
        finally:
            for img in rgb_images:
                try:
                    img.close()
                except Exception:
                    pass

    @staticmethod
    def _images_to_single_long_pdf(
        image_files: list[Path],
        task_dir: Path,
        album_id: str,
        profile: str,
        long_page_max_images: int,
        long_page_max_height: int,
    ) -> Path:
        if len(image_files) > long_page_max_images:
            return PackageService.images_to_pdf(
                image_files=image_files,
                task_dir=task_dir,
                album_id=album_id,
                profile=profile,
                layout_mode="multipage",
            )

        frames: list[Image.Image] = []
        try:
            max_width = 0
            total_height = 0
            for image_path in image_files:
                with Image.open(image_path) as img:
                    normalized = PackageService._normalize_image_for_profile(img, profile)
                    frame = normalized.copy()
                    normalized.close()
                    frames.append(frame)
                    max_width = max(max_width, frame.size[0])
                    total_height += frame.size[1]

            if total_height > long_page_max_height:
                return PackageService.images_to_pdf(
                    image_files=image_files,
                    task_dir=task_dir,
                    album_id=album_id,
                    profile=profile,
                    layout_mode="multipage",
                )

            long_image = Image.new("RGB", (max_width, total_height), color=(255, 255, 255))
            y = 0
            for frame in frames:
                x = max(0, (max_width - frame.size[0]) // 2)
                long_image.paste(frame, (x, y))
                y += frame.size[1]

            pdf_path = task_dir / f"{album_id}.pdf"
            long_image.save(pdf_path, "PDF", resolution=100.0)
            long_image.close()
            return pdf_path
        finally:
            for frame in frames:
                try:
                    frame.close()
                except Exception:
                    pass

    @staticmethod
    def _normalize_image_for_profile(img: Image.Image, profile: str) -> Image.Image:
        if img.mode != "RGB":
            img = img.convert("RGB")

        # fast: 更激进缩放减小体积；balanced/high: 保留原尺寸，避免拼接处出现缩放缝隙。
        max_edge = 0
        if profile == "fast":
            max_edge = 1600

        if max_edge > 0:
            width, height = img.size
            longest = max(width, height)
            if longest > max_edge:
                scale = max_edge / float(longest)
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

        return PackageService._trim_tiny_white_edges(img)

    @staticmethod
    def _trim_tiny_white_edges(img: Image.Image) -> Image.Image:
        """裁掉上下最多 2px 的纯白细边，降低连续阅读时的白缝感。"""
        if img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        if width <= 1 or height <= 2:
            return img.copy()

        max_trim = min(2, height // 8)
        top_trim = 0
        bottom_trim = 0

        for y in range(max_trim):
            if PackageService._is_white_row(img, y):
                top_trim += 1
            else:
                break

        for offset in range(max_trim):
            y = height - 1 - offset
            if y <= top_trim:
                break
            if PackageService._is_white_row(img, y):
                bottom_trim += 1
            else:
                break

        if top_trim == 0 and bottom_trim == 0:
            return img.copy()

        upper = top_trim
        lower = max(upper + 1, height - bottom_trim)
        return img.crop((0, upper, width, lower))

    @staticmethod
    def _is_white_row(img: Image.Image, y: int) -> bool:
        width = img.size[0]
        row = img.crop((0, y, width, y + 1)).getdata()
        white_count = 0
        for r, g, b in row:
            if r >= 248 and g >= 248 and b >= 248:
                white_count += 1
        return white_count / max(1, width) >= 0.995

    @staticmethod
    def rename_pdf_to_exe(pdf_path: Path) -> Path:
        if not pdf_path.exists():
            raise RuntimeError("PDF 文件不存在，无法转换后缀")

        exe_path = pdf_path.with_suffix(".exe")
        if exe_path.exists():
            exe_path.unlink()
        pdf_path.rename(exe_path)
        return exe_path

    @staticmethod
    def zip_with_password(payload_path: Path, task_dir: Path, album_id: str, zip_level: int, zip_password: str) -> Path:
        if not payload_path.exists():
            raise RuntimeError("待压缩文件不存在，无法压缩")
        if not zip_password:
            raise RuntimeError("未配置 zip_password，无法进行加密压缩")

        level = max(0, min(9, int(zip_level)))
        zip_path = task_dir / f"{album_id}.zip"

        with pyzipper.AESZipFile(
            zip_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            compresslevel=level,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(zip_password.encode("utf-8"))
            zf.write(payload_path, arcname=payload_path.name)
        return zip_path
