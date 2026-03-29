from pathlib import Path

import pyzipper
from PIL import Image


class PackageService:
    @staticmethod
    def images_to_pdf(image_files: list[Path], task_dir: Path, album_id: str) -> Path:
        if not image_files:
            raise RuntimeError("未找到可用于生成 PDF 的图片")

        rgb_images: list[Image.Image] = []
        try:
            for image_path in image_files:
                with Image.open(image_path) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    rgb_images.append(img.copy())

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
