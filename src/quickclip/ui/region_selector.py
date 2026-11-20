"""Full-monitor translucent overlay widget for interactive screen region selection."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QWidget

from quickclip.contracts import CaptureRegion, MonitorInfo

_MIN_LOGICAL_SELECTION_PX = 24


def logical_rect_to_capture_region(
    rect: QRect,
    overlay_size: QSize,
    monitor: MonitorInfo,
) -> CaptureRegion:
    """Convert widget-local logical ``rect`` into physical monitor-relative ``CaptureRegion``."""
    ov_w = max(1, int(overlay_size.width()))
    ov_h = max(1, int(overlay_size.height()))
    bounds = QRect(0, 0, ov_w, ov_h)
    clamped = rect.normalized().intersected(bounds)

    scale_x = monitor.width / ov_w
    scale_y = monitor.height / ov_h

    phys_left = round(clamped.left() * scale_x)
    phys_top = round(clamped.top() * scale_y)
    phys_w = max(2, round(clamped.width() * scale_x))
    phys_h = max(2, round(clamped.height() * scale_y))

    return CaptureRegion(
        left=max(0, phys_left),
        top=max(0, phys_top),
        width=phys_w,
        height=phys_h,
        monitor_index=monitor.index,
    ).normalized(monitor.width, monitor.height)


class RegionSelectorOverlay(QWidget):
    """Frameless translucent overlay allowing the user to drag-select a capture region."""

    regionSelected = Signal(CaptureRegion)
    selectionCancelled = Signal()

    def __init__(
        self,
        monitor: MonitorInfo,
        screen_geometry: QRect | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._monitor = monitor
        self._screen_geometry = (
            QRect(screen_geometry)
            if screen_geometry is not None and not screen_geometry.isEmpty()
            else QRect(monitor.left, monitor.top, monitor.width, monitor.height)
        )
        self._drag_origin: QPoint | None = None
        self._drag_current: QPoint | None = None
        self._finished = False
        self._hint_text = (
            "Kayıt alınacak alanı fare ile sürükleyip seçin • İptal: Esc veya Sağ Tık"
        )

        self.setWindowTitle("QuickClip — Alan Seç")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(self._screen_geometry)

    @property
    def monitor(self) -> MonitorInfo:
        """Return the target monitor metadata associated with this overlay."""
        return self._monitor

    def current_selection_rect(self) -> QRect | None:
        """Return the active widget-local normalized selection rectangle, if dragging."""
        if self._drag_origin is None or self._drag_current is None:
            return None
        x1, y1 = self._drag_origin.x(), self._drag_origin.y()
        x2, y2 = self._drag_current.x(), self._drag_current.y()
        return QRect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

    def start_selection(self) -> None:
        """Reset state, display the overlay across the target monitor, and focus it."""
        self._drag_origin = None
        self._drag_current = None
        self._finished = False
        self._hint_text = (
            "Kayıt alınacak alanı fare ile sürükleyip seçin • İptal: Esc veya Sağ Tık"
        )
        self.setGeometry(self._screen_geometry)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.update()

    def cancel_selection(self) -> None:
        """Close the overlay and emit ``selectionCancelled`` once."""
        if self._finished:
            return
        self._finished = True
        self._drag_origin = None
        self._drag_current = None
        self.hide()
        self.selectionCancelled.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._finished:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.cancel_selection()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pt = event.position().toPoint()
            self._drag_origin = pt
            self._drag_current = pt
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._finished and self._drag_origin is not None:
            self._drag_current = event.position().toPoint()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            not self._finished
            and event.button() == Qt.MouseButton.LeftButton
            and self._drag_origin is not None
        ):
            self._drag_current = event.position().toPoint()
            rect = self.current_selection_rect()
            if (
                rect is None
                or rect.width() < _MIN_LOGICAL_SELECTION_PX
                or rect.height() < _MIN_LOGICAL_SELECTION_PX
            ):
                self._drag_origin = None
                self._drag_current = None
                self._hint_text = (
                    "Alan çok küçük — fareyle daha geniş bir dikdörtgen sürükleyin (Esc: İptal)"
                )
                self.update()
                event.accept()
                return

            region = logical_rect_to_capture_region(rect, self.size(), self._monitor)
            self._finished = True
            self.hide()
            self.regionSelected.emit(region)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        full_rect = QRectF(self.rect())
        veil_path = QPainterPath()
        veil_path.addRect(full_rect)

        sel_rect = self.current_selection_rect()
        if sel_rect is not None and sel_rect.width() > 1 and sel_rect.height() > 1:
            cutout = QPainterPath()
            cutout.addRect(QRectF(sel_rect))
            veil_path = veil_path.subtracted(cutout)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(10, 14, 22, 135))
        painter.drawPath(veil_path)

        # Top centered instruction pill.
        font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        pill_w = min(self.width() - 32, fm.horizontalAdvance(self._hint_text) + 32)
        pill_h = 34
        pill_rect = QRectF((self.width() - pill_w) / 2.0, 18.0, pill_w, pill_h)
        painter.setBrush(QColor(18, 22, 32, 225))
        painter.setPen(QPen(QColor(59, 130, 246, 200), 1.2))
        painter.drawRoundedRect(pill_rect, 8.0, 8.0)
        painter.setPen(QColor("#f8fafc"))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self._hint_text)

        if sel_rect is not None and sel_rect.width() > 1 and sel_rect.height() > 1:
            border_rect = QRectF(sel_rect)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#3b82f6"), 2.0))
            painter.drawRect(border_rect)

            # Corner accents for clear visual framing.
            painter.setPen(QPen(QColor("#93c5fd"), 3.0))
            arm = min(14.0, border_rect.width() / 3.0, border_rect.height() / 3.0)
            l, t, r, b = border_rect.left(), border_rect.top(), border_rect.right(), border_rect.bottom()
            for x1, y1, x2, y2 in (
                (l, t, l + arm, t),
                (l, t, l, t + arm),
                (r, t, r - arm, t),
                (r, t, r, t + arm),
                (l, b, l + arm, b),
                (l, b, l, b - arm),
                (r, b, r - arm, b),
                (r, b, r, b - arm),
            ):
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))

            # Floating physical dimension badge.
            region = logical_rect_to_capture_region(sel_rect, self.size(), self._monitor)
            dim_text = f"{region.width} × {region.height} px"
            badge_font = QFont("Consolas", 10, QFont.Weight.Bold)
            painter.setFont(badge_font)
            bfm = painter.fontMetrics()
            bw = bfm.horizontalAdvance(dim_text) + 18
            bh = 26
            bx = max(8.0, min(l, self.width() - bw - 8.0))
            by = t - bh - 6.0 if t >= bh + 12.0 else min(b + 6.0, self.height() - bh - 8.0)
            badge_rect = QRectF(bx, by, bw, bh)
            painter.setBrush(QColor(15, 23, 42, 235))
            painter.setPen(QPen(QColor("#3b82f6"), 1.0))
            painter.drawRoundedRect(badge_rect, 5.0, 5.0)
            painter.setPen(QColor("#e2e8f0"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, dim_text)

        painter.end()


__all__ = ["RegionSelectorOverlay", "logical_rect_to_capture_region"]
