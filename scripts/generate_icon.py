"""Generate multi-resolution QuickClip Windows icon and PNG asset via PySide6."""

from __future__ import annotations

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen, QRadialGradient

ICON_SIZES: list[int] = [16, 24, 32, 48, 64, 128, 256]


def render_icon_image(size: int) -> QImage:
    """Render a crisp QuickClip icon at ``size x size`` ARGB32 pixels."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    s = size / 256.0
    pad = max(1.0, 12.0 * s)
    outer_rect = QRectF(pad, pad, size - 2.0 * pad, size - 2.0 * pad)
    radius = 48.0 * s

    # Dark slate/navy rounded base with subtle depth gradient.
    base_grad = QLinearGradient(outer_rect.topLeft(), outer_rect.bottomRight())
    base_grad.setColorAt(0.0, QColor("#162136"))
    base_grad.setColorAt(1.0, QColor("#0b1120"))
    painter.setBrush(base_grad)

    border_width = max(1.5, 12.0 * s)
    border_grad = QLinearGradient(outer_rect.topLeft(), outer_rect.bottomRight())
    border_grad.setColorAt(0.0, QColor("#38bdf8"))
    border_grad.setColorAt(1.0, QColor("#2563eb"))
    painter.setPen(QPen(border_grad, border_width))
    painter.drawRoundedRect(outer_rect, radius, radius)

    # Subtle viewfinder / clip corner brackets for larger icon sizes.
    if size >= 24:
        inset = max(4.0, 44.0 * s)
        bracket_len = max(3.0, 32.0 * s)
        left = inset
        top = inset
        right = size - inset
        bottom = size - inset
        bracket_pen = QPen(QColor(96, 165, 250, 190), max(1.2, 8.0 * s))
        bracket_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(bracket_pen)

        # Top-left
        painter.drawLine(QPointF(left, top + bracket_len), QPointF(left, top))
        painter.drawLine(QPointF(left, top), QPointF(left + bracket_len, top))
        # Top-right
        painter.drawLine(QPointF(right - bracket_len, top), QPointF(right, top))
        painter.drawLine(QPointF(right, top), QPointF(right, top + bracket_len))
        # Bottom-left
        painter.drawLine(QPointF(left, bottom - bracket_len), QPointF(left, bottom))
        painter.drawLine(QPointF(left, bottom), QPointF(left + bracket_len, bottom))
        # Bottom-right
        painter.drawLine(QPointF(right - bracket_len, bottom), QPointF(right, bottom))
        painter.drawLine(QPointF(right, bottom), QPointF(right, bottom - bracket_len))

    # Crisp crimson record dot in the center.
    center = QPointF(size / 2.0, size / 2.0)
    dot_radius = max(3.5, 52.0 * s)
    dot_rect = QRectF(
        center.x() - dot_radius,
        center.y() - dot_radius,
        dot_radius * 2.0,
        dot_radius * 2.0,
    )

    if size >= 32:
        glow_radius = dot_radius * 1.45
        glow = QRadialGradient(center, glow_radius)
        glow.setColorAt(0.0, QColor(239, 68, 68, 95))
        glow.setColorAt(1.0, QColor(239, 68, 68, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

    dot_grad = QLinearGradient(dot_rect.topLeft(), dot_rect.bottomRight())
    dot_grad.setColorAt(0.0, QColor("#f87171"))
    dot_grad.setColorAt(0.45, QColor("#ef4444"))
    dot_grad.setColorAt(1.0, QColor("#b91c1c"))
    painter.setPen(QPen(QColor("#7f1d1d"), max(1.0, 4.0 * s)))
    painter.setBrush(dot_grad)
    painter.drawEllipse(dot_rect)

    # Tiny specular highlight for a clean modern finish.
    if size >= 32:
        spec_r = max(1.5, 13.0 * s)
        spec_center = QPointF(center.x() - dot_radius * 0.32, center.y() - dot_radius * 0.32)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 110))
        painter.drawEllipse(spec_center, spec_r, spec_r)

    painter.end()
    return img


def image_to_png_bytes(image: QImage) -> bytes:
    """Encode a ``QImage`` as PNG bytes in memory."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = image.save(buf, "PNG")
    buf.close()
    if not ok:
        raise RuntimeError("Failed encoding QImage to PNG.")
    return bytes(buf.data())


def pack_ico(frames: list[tuple[int, bytes]]) -> bytes:
    """Pack ``[(size, png_bytes), ...]`` into a multi-resolution Windows ``.ico`` payload."""
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries: list[bytes] = []
    offset = 6 + 16 * len(frames)

    for size, png_bytes in frames:
        dim = 0 if size >= 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dim,
                dim,
                0,
                0,
                1,
                32,
                len(png_bytes),
                offset,
            )
        )
        offset += len(png_bytes)

    return header + b"".join(entries) + b"".join(png for _, png in frames)


def generate_assets(assets_dir: Path | None = None) -> tuple[Path, Path]:
    """Generate ``assets/quickclip.ico`` and ``assets/quickclip.png``."""
    target_dir = (
        Path(assets_dir)
        if assets_dir is not None
        else Path(__file__).resolve().parents[1] / "assets"
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    frames: list[tuple[int, bytes]] = []
    png_256: bytes | None = None
    for size in ICON_SIZES:
        png_bytes = image_to_png_bytes(render_icon_image(size))
        frames.append((size, png_bytes))
        if size == 256:
            png_256 = png_bytes

    ico_path = target_dir / "quickclip.ico"
    png_path = target_dir / "quickclip.png"
    ico_path.write_bytes(pack_ico(frames))
    assert png_256 is not None
    png_path.write_bytes(png_256)
    return ico_path, png_path


if __name__ == "__main__":
    ico, png = generate_assets()
    print(f"Generated {ico} ({ico.stat().st_size} bytes) and {png} ({png.stat().st_size} bytes)")
