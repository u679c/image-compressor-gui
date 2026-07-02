from __future__ import annotations

import io
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps
from PyQt5.QtCore import QRect, QSize, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".gif",
    ".jp2",
    ".j2k",
    ".ico",
}
ProgressCallback = Callable[[int, str], None]
QUALITY_FORMATS = {"JPEG", "WEBP"}

OUTPUT_FORMATS = [
    ("AUTO", "", "自动（推荐）", "自动 推荐 智能"),
    ("JPEG", ".jpg", "JPG / JPEG（照片，小体积）", "jpg jpeg 照片 有损 小体积"),
    ("PNG", ".png", "PNG（透明，无损）", "png 透明 无损"),
    ("WEBP", ".webp", "WebP（小体积，支持透明）", "webp 网页 小体积 透明"),
    ("BMP", ".bmp", "BMP（位图，无压缩）", "bmp 位图 无压缩"),
    ("TIFF", ".tiff", "TIFF（印刷/归档）", "tif tiff 印刷 归档 无损"),
    ("GIF", ".gif", "GIF（兼容格式）", "gif 动图 兼容"),
    ("JPEG2000", ".jp2", "JPEG 2000（JP2）", "jp2 jpeg2000 jpeg 2000"),
    ("ICO", ".ico", "ICO（图标）", "ico icon 图标"),
]


@dataclass
class CompressResult:
    data: bytes
    extension: str
    format_name: str
    size_bytes: int
    dimensions: tuple[int, int]
    quality: int | None


def readable_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.2f} MB"


def has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )


def normalize_for_format(image: Image.Image, format_name: str) -> Image.Image:
    image = ImageOps.exif_transpose(image)

    if format_name in {"JPEG", "JPEG2000", "BMP"}:
        if has_alpha(image):
            background = Image.new("RGB", image.size, "white")
            background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
            return background
        return image.convert("RGB")

    if format_name in {"WEBP", "PNG", "TIFF", "ICO"}:
        return image.convert("RGBA" if has_alpha(image) else "RGB")

    if format_name == "GIF":
        return image.convert("P", palette=Image.Palette.ADAPTIVE)

    return image


def available_output_formats() -> list[tuple[str, str, str, str]]:
    Image.init()
    return [
        output_format
        for output_format in OUTPUT_FORMATS
        if output_format[0] == "AUTO" or output_format[0] in Image.SAVE
    ]


def format_extension(format_name: str) -> str:
    for candidate, extension, _label, _keywords in OUTPUT_FORMATS:
        if candidate == format_name:
            return extension
    return ".png"


def choose_output_format(requested: str, image: Image.Image) -> tuple[str, str]:
    if requested == "AUTO":
        available = {format_name for format_name, _extension, _label, _keywords in available_output_formats()}
        if has_alpha(image):
            if "WEBP" in available:
                return "WEBP", ".webp"
            return "PNG", ".png"
        if "JPEG" in available:
            return "JPEG", ".jpg"
        return "PNG", ".png"

    return requested, format_extension(requested)


def save_to_bytes(image: Image.Image, format_name: str, quality: int | None) -> bytes:
    buffer = io.BytesIO()

    if format_name == "JPEG":
        image.save(
            buffer,
            format="JPEG",
            quality=quality or 85,
            optimize=False,
            progressive=False,
            subsampling="4:2:0",
        )
    elif format_name == "WEBP":
        image.save(buffer, format="WEBP", quality=quality or 85, method=2)
    elif format_name == "PNG":
        image.save(buffer, format="PNG", optimize=False, compress_level=4)
    elif format_name == "TIFF":
        image.save(buffer, format="TIFF", compression="tiff_deflate")
    elif format_name == "GIF":
        image.save(buffer, format="GIF", optimize=True)
    elif format_name == "JPEG2000":
        image.save(buffer, format="JPEG2000", quality_mode="rates", quality_layers=[max(5, 100 - (quality or 70))])
    elif format_name == "ICO":
        image.save(buffer, format="ICO")
    else:
        image.save(buffer, format=format_name)

    return buffer.getvalue()


def quality_search(
    image: Image.Image,
    format_name: str,
    target_bytes: int,
    rounds: int = 4,
    progress_cb: ProgressCallback | None = None,
    progress_start: int = 0,
    progress_end: int = 100,
) -> tuple[bytes, int | None]:
    if format_name not in QUALITY_FORMATS:
        if progress_cb:
            progress_cb(progress_end, f"正在导出 {format_name}")
        return save_to_bytes(image, format_name, None), None

    best_data = save_to_bytes(image, format_name, 55)
    best_quality = 55
    best_distance = abs(len(best_data) - target_bytes)

    low, high = 25, 92
    for index in range(rounds):
        quality = (low + high) // 2
        data = save_to_bytes(image, format_name, quality)
        distance = abs(len(data) - target_bytes)
        if progress_cb:
            percent = progress_start + int((index + 1) / max(rounds, 1) * (progress_end - progress_start))
            progress_cb(percent, f"正在尝试质量 {quality}")

        if len(data) <= target_bytes and (
            len(best_data) > target_bytes or distance < best_distance
        ):
            best_data = data
            best_quality = quality
            best_distance = distance
        elif len(best_data) > target_bytes and distance < best_distance:
            best_data = data
            best_quality = quality
            best_distance = distance

        if len(data) > target_bytes:
            high = quality - 1
        else:
            low = quality + 1

    return best_data, best_quality


def quick_quality_guess(
    image: Image.Image,
    format_name: str,
    target_bytes: int,
    progress_cb: ProgressCallback | None = None,
    progress_start: int = 0,
    progress_end: int = 100,
) -> tuple[bytes, int | None]:
    if format_name not in QUALITY_FORMATS:
        if progress_cb:
            progress_cb(progress_end, f"正在导出 {format_name}")
        return save_to_bytes(image, format_name, None), None

    samples: list[tuple[bytes, int]] = []
    qualities = (82, 58, 36)
    for index, quality in enumerate(qualities):
        data = save_to_bytes(image, format_name, quality)
        samples.append((data, quality))
        if progress_cb:
            percent = progress_start + int((index + 1) / len(qualities) * (progress_end - progress_start))
            progress_cb(percent, f"正在快速估算质量 {quality}")
        if len(data) <= target_bytes * 1.12:
            return data, quality

    return min(samples, key=lambda item: abs(len(item[0]) - target_bytes))


def resize_down(image: Image.Image, scale: float) -> Image.Image:
    width, height = image.size
    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))
    if (new_width, new_height) == image.size:
        return image
    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def compress_image(
    path: str,
    target_bytes: int,
    requested_format: str,
    progress_cb: ProgressCallback | None = None,
) -> CompressResult:
    def report(percent: int, message: str) -> None:
        if progress_cb:
            progress_cb(max(0, min(100, percent)), message)

    report(2, "正在读取图片")
    with Image.open(path) as source:
        format_name, extension = choose_output_format(requested_format, source)
        report(8, "正在整理图片方向和颜色")
        working = normalize_for_format(source, format_name)

    original_area = working.width * working.height
    report(12, "正在估算压缩参数")
    best_data, best_quality = quick_quality_guess(
        working,
        format_name,
        target_bytes,
        progress_cb=progress_cb,
        progress_start=12,
        progress_end=42,
    )
    best_image = working

    if best_quality is not None and len(best_data) <= target_bytes * 1.4:
        report(45, "正在微调压缩质量")
        refined_data, refined_quality = quality_search(
            working,
            format_name,
            target_bytes,
            rounds=3,
            progress_cb=progress_cb,
            progress_start=45,
            progress_end=62,
        )
        if abs(len(refined_data) - target_bytes) < abs(len(best_data) - target_bytes):
            best_data = refined_data
            best_quality = refined_quality

    # If quality changes are not enough, resize once or twice instead of doing many slow passes.
    attempts = 0
    while len(best_data) > target_bytes * 1.2 and attempts < 3:
        report(66 + attempts * 9, "正在缩小尺寸")
        ratio = math.sqrt(target_bytes / max(len(best_data), 1))
        scale = min(0.9, max(0.45, ratio * 0.96))
        next_image = resize_down(best_image, scale)
        if next_image.width * next_image.height >= best_image.width * best_image.height:
            break

        candidate_data, candidate_quality = quality_search(
            next_image,
            format_name,
            target_bytes,
            rounds=3,
            progress_cb=progress_cb,
            progress_start=70 + attempts * 8,
            progress_end=78 + attempts * 8,
        )
        best_data = candidate_data
        best_quality = candidate_quality
        best_image = next_image
        attempts += 1

        if best_image.width * best_image.height < original_area * 0.01:
            break

    report(96, "正在生成压缩结果")
    return CompressResult(
        data=best_data,
        extension=extension,
        format_name=format_name,
        size_bytes=len(best_data),
        dimensions=best_image.size,
        quality=best_quality,
    )


class DropArea(QFrame):
    fileDropped = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("dropArea")
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        title = QLabel("拖动图片到这里")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("支持 JPG、PNG、WebP、BMP、TIFF、GIF、JP2、ICO")
        subtitle.setObjectName("dropSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)

        layout.addWidget(title)
        layout.addWidget(subtitle)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._first_supported_path(event):
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

        path = self._first_supported_path(event)
        if path:
            self.fileDropped.emit(path)
            event.acceptProposedAction()

    def _first_supported_path(self, event) -> str | None:
        if not event.mimeData().hasUrls():
            return None

        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).suffix.lower() in SUPPORTED_EXTENSIONS:
                return path
        return None


class CompareView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.original_pixmap = QPixmap()
        self.result_pixmap = QPixmap()
        self.mode = "side"
        self.swipe_ratio = 0.5
        self.onion_opacity = 0.5
        self.fit_to_window = True
        self.zoom_scale = 1.0
        self.pan_x = 0
        self.pan_y = 0
        self.last_drag_pos = None
        self.setMinimumHeight(320)
        self.setMouseTracking(True)
        self.setObjectName("compareView")

    def set_images(self, original: QPixmap | None, result: QPixmap | None) -> None:
        self.original_pixmap = original or QPixmap()
        self.result_pixmap = result or QPixmap()
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.update()

    def set_onion_opacity(self, value: int) -> None:
        self.onion_opacity = max(0.0, min(1.0, value / 100))
        self.update()

    def set_fit_to_window(self) -> None:
        self.fit_to_window = True
        self.pan_x = 0
        self.pan_y = 0
        self.update()

    def set_zoom_scale(self, scale: float) -> None:
        self.fit_to_window = False
        self.zoom_scale = max(0.1, min(4.0, scale))
        self.update()

    def zoom_in(self) -> None:
        current = self.current_zoom_scale(self.shared_image_rect())
        self.set_zoom_scale(current * 1.25)

    def zoom_out(self) -> None:
        current = self.current_zoom_scale(self.shared_image_rect())
        self.set_zoom_scale(current / 1.25)

    def zoom_text(self) -> str:
        if self.fit_to_window:
            return "适应窗口"
        return f"{round(self.zoom_scale * 100)}%"

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        painter.fillRect(self.rect(), QColor("#ffffff"))

        if self.original_pixmap.isNull():
            painter.setPen(QColor("#7b8794"))
            painter.drawText(self.rect(), Qt.AlignCenter, "请先拖入或选择一张图片")
            return

        if self.mode == "side":
            self.paint_side_by_side(painter)
        elif self.mode == "swipe":
            self.paint_swipe(painter)
        else:
            self.paint_onion(painter)

    def mousePressEvent(self, event) -> None:
        if self.mode == "swipe" and event.buttons() & Qt.LeftButton:
            self.update_swipe_ratio(event.pos().x())
        elif not self.fit_to_window and event.buttons() & Qt.LeftButton:
            self.last_drag_pos = event.pos()

    def mouseMoveEvent(self, event) -> None:
        if self.mode == "swipe" and event.buttons() & Qt.LeftButton:
            self.update_swipe_ratio(event.pos().x())
        elif not self.fit_to_window and event.buttons() & Qt.LeftButton and self.last_drag_pos:
            delta = event.pos() - self.last_drag_pos
            self.pan_x += delta.x()
            self.pan_y += delta.y()
            self.last_drag_pos = event.pos()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self.last_drag_pos = None

    def update_swipe_ratio(self, x: int) -> None:
        image_rect = self.shared_image_rect()
        if image_rect.width() <= 0:
            return
        self.swipe_ratio = max(0.03, min(0.97, (x - image_rect.left()) / image_rect.width()))
        self.update()

    def paint_side_by_side(self, painter: QPainter) -> None:
        canvas = self.canvas_rect()
        gap = 16
        half_width = max(1, (canvas.width() - gap) // 2)
        left_rect = QRect(canvas.left(), canvas.top(), half_width, canvas.height())
        right_rect = QRect(left_rect.right() + gap + 1, canvas.top(), half_width, canvas.height())

        self.paint_label(painter, left_rect, "原图", QColor("#ef4444"))
        self.paint_label(painter, right_rect, "压缩后", QColor("#22c55e"))

        left_image_rect = left_rect.adjusted(0, 34, 0, 0)
        right_image_rect = right_rect.adjusted(0, 34, 0, 0)
        self.draw_image_box(painter, left_image_rect, self.original_pixmap)
        self.draw_image_box(painter, right_image_rect, self.result_pixmap)

    def paint_swipe(self, painter: QPainter) -> None:
        canvas = self.canvas_rect()
        self.paint_label(painter, canvas, "原图", QColor("#ef4444"), align_left=True)
        self.paint_label(painter, canvas, "压缩后", QColor("#22c55e"), align_left=False)

        image_rect = self.shared_image_rect()
        self.draw_checkerboard(painter, image_rect)

        split_x = image_rect.left() + int(image_rect.width() * self.swipe_ratio)
        left_clip = QRect(
            image_rect.left(),
            image_rect.top(),
            max(0, split_x - image_rect.left()),
            image_rect.height(),
        )
        right_clip = QRect(
            split_x,
            image_rect.top(),
            max(0, image_rect.right() - split_x + 1),
            image_rect.height(),
        )

        painter.save()
        painter.setClipRect(left_clip)
        self.draw_pixmap_scaled(painter, image_rect, self.original_pixmap)
        painter.restore()

        painter.save()
        painter.setClipRect(right_clip)
        if self.result_pixmap.isNull():
            self.paint_missing_result(painter, right_clip)
        else:
            self.draw_pixmap_scaled(painter, image_rect, self.result_pixmap)
        painter.restore()

        self.draw_image_border(painter, image_rect)
        painter.setPen(QPen(QColor("#1677ff"), 2))
        painter.drawLine(split_x, image_rect.top() - 8, split_x, image_rect.bottom() + 8)
        painter.setBrush(QColor("#1677ff"))
        painter.setPen(QColor("#1677ff"))
        painter.drawEllipse(split_x - 5, image_rect.center().y() - 5, 10, 10)

    def paint_onion(self, painter: QPainter) -> None:
        canvas = self.canvas_rect()
        self.paint_label(painter, canvas, "原图", QColor("#ef4444"), align_left=True)
        self.paint_label(painter, canvas, "压缩后", QColor("#22c55e"), align_left=False)

        image_rect = self.shared_image_rect()
        self.draw_checkerboard(painter, image_rect)
        self.draw_pixmap_scaled(painter, image_rect, self.original_pixmap)

        if not self.result_pixmap.isNull():
            painter.save()
            painter.setOpacity(self.onion_opacity)
            self.draw_pixmap_scaled(painter, image_rect, self.result_pixmap)
            painter.restore()
        else:
            self.paint_missing_result(painter, image_rect)

        self.draw_image_border(painter, image_rect)

    def canvas_rect(self) -> QRect:
        return self.rect().adjusted(16, 10, -16, -14)

    def shared_image_rect(self) -> QRect:
        return self.canvas_rect().adjusted(0, 34, 0, 0)

    def paint_label(
        self,
        painter: QPainter,
        rect: QRect,
        text: str,
        color: QColor,
        align_left: bool = True,
    ) -> None:
        label_rect = QRect(rect.left(), rect.top(), rect.width(), 28)
        painter.setPen(color)
        font = painter.font()
        font.setPointSize(16)
        font.setBold(True)
        painter.setFont(font)
        flags = Qt.AlignVCenter | (Qt.AlignLeft if align_left else Qt.AlignRight)
        painter.drawText(label_rect, flags, text)

    def draw_image_box(self, painter: QPainter, rect: QRect, pixmap: QPixmap) -> None:
        self.draw_checkerboard(painter, rect)
        if pixmap.isNull():
            self.paint_missing_result(painter, rect)
        else:
            self.draw_pixmap_scaled(painter, rect, pixmap)
        self.draw_image_border(painter, rect)

    def current_zoom_scale(self, rect: QRect) -> float:
        if self.original_pixmap.isNull():
            return 1.0
        fit_scale = min(
            rect.width() / max(self.original_pixmap.width(), 1),
            rect.height() / max(self.original_pixmap.height(), 1),
        )
        if self.fit_to_window:
            return max(0.01, fit_scale)
        return self.zoom_scale

    def target_image_rect(self, rect: QRect) -> QRect:
        if self.original_pixmap.isNull():
            return rect

        scale = self.current_zoom_scale(rect)
        width = max(1, int(self.original_pixmap.width() * scale))
        height = max(1, int(self.original_pixmap.height() * scale))
        x = rect.left() + (rect.width() - width) // 2 + self.pan_x
        y = rect.top() + (rect.height() - height) // 2 + self.pan_y
        return QRect(x, y, width, height)

    def draw_pixmap_scaled(self, painter: QPainter, rect: QRect, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return

        target = self.target_image_rect(rect)
        painter.save()
        painter.setClipRect(rect, Qt.IntersectClip)
        painter.drawPixmap(target, pixmap)
        painter.restore()

    def draw_checkerboard(self, painter: QPainter, rect: QRect) -> None:
        painter.fillRect(rect, QColor("#ffffff"))
        tile = 16
        light = QColor("#f1f3f5")
        dark = QColor("#d4d7dc")
        for y in range(rect.top(), rect.bottom() + 1, tile):
            for x in range(rect.left(), rect.right() + 1, tile):
                color = dark if ((x // tile) + (y // tile)) % 2 else light
                painter.fillRect(QRect(x, y, tile, tile), color)

    def draw_image_border(self, painter: QPainter, rect: QRect) -> None:
        painter.setPen(QPen(QColor("#aab2bd"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def paint_missing_result(self, painter: QPainter, rect: QRect) -> None:
        painter.setPen(QColor("#667085"))
        painter.drawText(rect, Qt.AlignCenter, "压缩后会显示在这里")


class CompressWorker(QThread):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, path: str, target_bytes: int, requested_format: str) -> None:
        super().__init__()
        self.path = path
        self.target_bytes = target_bytes
        self.requested_format = requested_format

    def run(self) -> None:
        try:
            self.finished.emit(
                compress_image(
                    self.path,
                    self.target_bytes,
                    self.requested_format,
                    progress_cb=lambda percent, message: self.progress.emit(percent, message),
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("图片压缩工具")
        self.resize(900, 640)

        self.current_path: str | None = None
        self.current_result: CompressResult | None = None
        self.original_pixmap = QPixmap()
        self.result_pixmap = QPixmap()
        self.worker: CompressWorker | None = None

        root = QWidget()
        self.setCentralWidget(root)

        main = QVBoxLayout(root)
        main.setContentsMargins(24, 24, 24, 24)
        main.setSpacing(16)

        header = QLabel("图片压缩工具")
        header.setObjectName("header")
        main.addWidget(header)

        self.drop_area = DropArea()
        self.drop_area.fileDropped.connect(self.load_image)
        main.addWidget(self.drop_area)

        controls = QHBoxLayout()
        controls.setSpacing(12)

        self.pick_button = QPushButton("选择图片")
        self.pick_button.clicked.connect(self.pick_image)
        controls.addWidget(self.pick_button)

        form_holder = QWidget()
        form = QFormLayout(form_holder)
        form.setContentsMargins(0, 0, 0, 0)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(1, 200)
        self.size_spin.setDecimals(1)
        self.size_spin.setValue(300)
        self.size_spin.setSuffix(" KB")

        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["KB", "MB"])
        self.unit_combo.currentTextChanged.connect(self.update_size_suffix)

        size_row = QHBoxLayout()
        size_row.addWidget(self.size_spin)
        size_row.addWidget(self.unit_combo)
        size_row_widget = QWidget()
        size_row_widget.setLayout(size_row)
        form.addRow("目标大小", size_row_widget)

        self.format_combo = QComboBox()
        self.setup_format_combo()
        form.addRow("导出格式", self.format_combo)

        controls.addWidget(form_holder, 1)

        self.compress_button = QPushButton("开始压缩")
        self.compress_button.setEnabled(False)
        self.compress_button.clicked.connect(self.start_compress)
        controls.addWidget(self.compress_button)

        self.save_button = QPushButton("下载 / 另存为")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_result)
        controls.addWidget(self.save_button)

        main.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("准备就绪")
        self.progress.setTextVisible(True)
        main.addWidget(self.progress)

        compare_panel = QFrame()
        compare_panel.setObjectName("previewPanel")
        compare_layout = QVBoxLayout(compare_panel)
        compare_layout.setSpacing(12)

        info_row = QHBoxLayout()
        info_row.setSpacing(12)
        self.original_info = QLabel("原图：-")
        self.original_info.setObjectName("info")
        self.result_info = QLabel("压缩后：-")
        self.result_info.setObjectName("info")
        self.result_info.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info_row.addWidget(self.original_info)
        info_row.addWidget(self.result_info)
        compare_layout.addLayout(info_row)

        self.compare_view = CompareView()
        compare_layout.addWidget(self.compare_view, 1)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        mode_row.addStretch(1)

        self.mode_buttons: dict[str, QPushButton] = {}
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        modes = [
            ("side", "并排对比"),
            ("swipe", "滑动对比"),
            ("onion", "叠加对比"),
        ]
        for mode, text in modes:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("modeButton")
            button.clicked.connect(lambda checked=False, selected=mode: self.set_compare_mode(selected))
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            mode_row.addWidget(button)

        self.opacity_label = QLabel("叠加强度")
        self.opacity_label.setObjectName("info")
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(50)
        self.opacity_slider.setFixedWidth(130)
        self.opacity_slider.valueChanged.connect(self.compare_view.set_onion_opacity)
        mode_row.addWidget(self.opacity_label)
        mode_row.addWidget(self.opacity_slider)

        self.zoom_label = QLabel("缩放：适应窗口")
        self.zoom_label.setObjectName("info")
        mode_row.addSpacing(10)
        mode_row.addWidget(self.zoom_label)

        self.fit_button = QPushButton("适应窗口")
        self.fit_button.setObjectName("modeButton")
        self.fit_button.clicked.connect(self.fit_compare_view)
        mode_row.addWidget(self.fit_button)

        self.actual_button = QPushButton("100%")
        self.actual_button.setObjectName("modeButton")
        self.actual_button.clicked.connect(self.actual_size_compare_view)
        mode_row.addWidget(self.actual_button)

        self.zoom_out_button = QPushButton("缩小")
        self.zoom_out_button.setObjectName("modeButton")
        self.zoom_out_button.clicked.connect(self.zoom_compare_out)
        mode_row.addWidget(self.zoom_out_button)

        self.zoom_in_button = QPushButton("放大")
        self.zoom_in_button.setObjectName("modeButton")
        self.zoom_in_button.clicked.connect(self.zoom_compare_in)
        mode_row.addWidget(self.zoom_in_button)

        mode_row.addStretch(1)
        compare_layout.addLayout(mode_row)
        main.addWidget(compare_panel, 1)
        self.set_compare_mode("side")

        self.status = QLabel("请选择或拖入一张图片。")
        self.status.setObjectName("status")
        main.addWidget(self.status)

        self.apply_style()

    def setup_format_combo(self) -> None:
        self.format_combo.setEditable(True)
        self.format_combo.setInsertPolicy(QComboBox.NoInsert)
        self.format_combo.setMaxVisibleItems(12)

        for format_name, extension, label, keywords in available_output_formats():
            search_text = f"{label}  {extension}  {keywords}".strip()
            self.format_combo.addItem(search_text, format_name)

        completer = QCompleter(self.format_combo.model(), self.format_combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.format_combo.setCompleter(completer)
        self.format_combo.setCurrentIndex(0)
        if self.format_combo.lineEdit():
            self.format_combo.lineEdit().setPlaceholderText("搜索格式，如 jpg / png / 透明")

    def selected_output_format(self) -> str:
        text = self.format_combo.currentText().strip().lower()
        current_item = self.format_combo.itemText(self.format_combo.currentIndex()).strip().lower()
        should_search_text = bool(text) and text != current_item

        if not should_search_text:
            current_data = self.format_combo.currentData()
            if current_data:
                return str(current_data)

        for format_name, extension, label, keywords in available_output_formats():
            haystack = f"{format_name} {extension} {label} {keywords}".lower()
            if text and text in haystack:
                return format_name

        return "AUTO"

    def set_compare_mode(self, mode: str) -> None:
        self.compare_view.set_mode(mode)
        for button_mode, button in self.mode_buttons.items():
            selected = button_mode == mode
            button.setChecked(selected)
            button.setProperty("selected", selected)
            button.style().unpolish(button)
            button.style().polish(button)

        show_opacity = mode == "onion"
        self.opacity_label.setVisible(show_opacity)
        self.opacity_slider.setVisible(show_opacity)

    def update_zoom_label(self) -> None:
        self.zoom_label.setText(f"缩放：{self.compare_view.zoom_text()}")

    def fit_compare_view(self) -> None:
        self.compare_view.set_fit_to_window()
        self.update_zoom_label()

    def actual_size_compare_view(self) -> None:
        self.compare_view.set_zoom_scale(1.0)
        self.update_zoom_label()

    def zoom_compare_in(self) -> None:
        self.compare_view.zoom_in()
        self.update_zoom_label()

    def zoom_compare_out(self) -> None:
        self.compare_view.zoom_out()
        self.update_zoom_label()

    def apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                background: #f6f7f9;
                color: #1f2933;
                font-family: "PingFang SC", "Microsoft YaHei", Arial;
                font-size: 14px;
            }
            #header {
                font-size: 28px;
                font-weight: 700;
            }
            #dropArea {
                background: #ffffff;
                border: 2px dashed #8aa2bd;
                border-radius: 8px;
            }
            #dropArea[dragging="true"] {
                background: #eef7ff;
                border-color: #1677ff;
            }
            #dropTitle {
                background: transparent;
                font-size: 21px;
                font-weight: 700;
            }
            #dropSubtitle {
                background: transparent;
                color: #667085;
            }
            QPushButton {
                background: #1677ff;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 16px;
                font-weight: 600;
            }
            QPushButton:disabled {
                background: #b8c2cc;
            }
            QPushButton:hover:!disabled {
                background: #0f66dd;
            }
            QDoubleSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #c7d0da;
                border-radius: 6px;
                padding: 7px 8px;
                min-width: 88px;
            }
            #previewPanel {
                background: #ffffff;
                border: 1px solid #d9e0e8;
                border-radius: 8px;
            }
            #compareView {
                background: #ffffff;
                border: 1px solid #edf0f3;
                border-radius: 6px;
            }
            #panelTitle {
                background: transparent;
                font-weight: 700;
            }
            #imagePreview {
                background: #f9fafb;
                border: 1px solid #edf0f3;
                border-radius: 6px;
                color: #7b8794;
            }
            #info, #status {
                background: transparent;
                color: #52606d;
            }
            #modeButton {
                background: transparent;
                color: #6b7280;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 700;
            }
            #modeButton:hover {
                background: #eef2f7;
                color: #1f2933;
            }
            #modeButton[selected="true"] {
                background: #d9dde3;
                color: #111827;
                border-color: #d9dde3;
            }
            QSlider {
                background: transparent;
            }
            QSlider::groove:horizontal {
                background: #d9e0e8;
                border-radius: 3px;
                height: 6px;
            }
            QSlider::handle:horizontal {
                background: #1677ff;
                border-radius: 7px;
                width: 14px;
                margin: -4px 0;
            }
            QProgressBar {
                background: #e4e7eb;
                border: none;
                border-radius: 4px;
                height: 22px;
                color: #1f2933;
                text-align: center;
                font-weight: 600;
            }
            QProgressBar::chunk {
                background: #1677ff;
                border-radius: 4px;
            }
            """
        )

    def update_size_suffix(self, unit: str) -> None:
        self.size_spin.setSuffix(f" {unit}")
        self.size_spin.setRange(1, 200 if unit == "MB" else 200000)
        if unit == "KB" and self.size_spin.value() > 200000:
            self.size_spin.setValue(300)

    def pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            str(Path.home()),
            "Images (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif *.jp2 *.j2k *.ico)",
        )
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> None:
        if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            QMessageBox.warning(self, "不支持的文件", "请选择常见图片文件，如 JPG、PNG、WebP、BMP、TIFF、GIF、JP2 或 ICO。")
            return

        self.current_path = path
        self.current_result = None
        self.result_pixmap = QPixmap()
        self.save_button.setEnabled(False)
        self.compress_button.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setFormat("准备压缩")

        self.original_pixmap = QPixmap(path)
        self.compare_view.set_images(self.original_pixmap, None)
        self.fit_compare_view()

        with Image.open(path) as image:
            size = os.path.getsize(path)
            self.original_info.setText(
                f"原图：{readable_size(size)}  |  {image.width} x {image.height}"
            )

        self.result_info.setText("压缩后：-")
        self.status.setText(f"已载入：{Path(path).name}")

    def target_bytes(self) -> int:
        multiplier = 1024 * 1024 if self.unit_combo.currentText() == "MB" else 1024
        return int(self.size_spin.value() * multiplier)

    def start_compress(self) -> None:
        if not self.current_path:
            return

        self.compress_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0% 正在准备")
        self.status.setText("正在压缩...")

        requested_format = self.selected_output_format()
        self.worker = CompressWorker(self.current_path, self.target_bytes(), requested_format)
        self.worker.progress.connect(self.on_compress_progress)
        self.worker.finished.connect(self.on_compress_finished)
        self.worker.failed.connect(self.on_compress_failed)
        self.worker.start()

    def on_compress_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.progress.setFormat(f"{percent}% {message}")
        self.status.setText(message)

    def on_compress_finished(self, result: CompressResult) -> None:
        self.current_result = result
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setFormat("100% 压缩完成")
        self.compress_button.setEnabled(True)
        self.save_button.setEnabled(True)

        self.result_pixmap = QPixmap()
        self.result_pixmap.loadFromData(result.data)
        self.compare_view.set_images(self.original_pixmap, self.result_pixmap)

        quality_text = f"  |  质量 {result.quality}" if result.quality is not None else ""
        self.result_info.setText(
            f"压缩后：{readable_size(result.size_bytes)}  |  {result.dimensions[0]} x {result.dimensions[1]}"
            f"  |  {result.format_name}{quality_text}"
        )

        target = self.target_bytes()
        diff = (result.size_bytes - target) / target * 100
        self.status.setText(f"压缩完成，结果与目标相差约 {diff:+.1f}%。")

    def on_compress_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("压缩失败")
        self.compress_button.setEnabled(True)
        QMessageBox.critical(self, "压缩失败", message)
        self.status.setText("压缩失败，请换一张图片再试。")

    def save_result(self) -> None:
        if not self.current_result or not self.current_path:
            return

        source = Path(self.current_path)
        default_name = source.with_name(f"{source.stem}_compressed{self.current_result.extension}")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存压缩图片",
            str(default_name),
            f"{self.current_result.format_name} (*{self.current_result.extension})",
        )
        if not path:
            return

        save_path = Path(path)
        if save_path.suffix.lower() != self.current_result.extension:
            save_path = save_path.with_suffix(self.current_result.extension)
        save_path.write_bytes(self.current_result.data)
        self.status.setText(f"已保存：{save_path}")


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
