"""Dual-handle range slider widget with playhead cursor for QuickClip trimming."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

_ActiveHandle = Literal["start", "end", "playhead"]


class RangeSlider(QWidget):
    """Dark-themed timeline range slider with start/end trim handles and playhead.

    Enforces ``0 <= start_ms < end_ms <= duration_ms`` whenever ``duration_ms > 0``,
    keeping at least ``min_span_ms`` between the trim handles when duration allows.
    """

    rangeChanged = Signal(int, int)
    seekRequested = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        min_span_ms: int = 200,
    ) -> None:
        super().__init__(parent)
        if min_span_ms < 1:
            raise ValueError(f"min_span_ms must be >= 1, got {min_span_ms}")

        self._min_span_ms: int = int(min_span_ms)
        self._duration_ms: int = 0
        self._start_ms: int = 0
        self._end_ms: int = 0
        self._position_ms: int = 0

        self._active_handle: _ActiveHandle | None = None
        self._hovered_handle: _ActiveHandle | None = None

        self._margin_x: int = 14
        self._handle_hit_radius: int = 12
        self._track_height: int = 8

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(46)

    def sizeHint(self) -> QSize:
        """Return preferred widget dimensions."""
        return QSize(480, 48)

    def minimumSizeHint(self) -> QSize:
        """Return minimum usable widget dimensions."""
        return QSize(180, 44)

    def duration_ms(self) -> int:
        """Return total timeline duration in milliseconds."""
        return self._duration_ms

    def start_ms(self) -> int:
        """Return trim start timestamp in milliseconds."""
        return self._start_ms

    def end_ms(self) -> int:
        """Return trim end timestamp in milliseconds."""
        return self._end_ms

    def position_ms(self) -> int:
        """Return current playhead timestamp in milliseconds."""
        return self._position_ms

    def set_duration(self, duration_ms: int) -> None:
        """Set total media duration in milliseconds and clamp existing selection."""
        dur = max(0, int(duration_ms))
        old_dur = self._duration_ms
        old_range = (self._start_ms, self._end_ms)

        self._duration_ms = dur
        if dur == 0:
            self._start_ms = 0
            self._end_ms = 0
            self._position_ms = 0
        elif old_dur == 0:
            self._start_ms = 0
            self._end_ms = dur
            self._position_ms = 0
        else:
            if old_range == (0, old_dur):
                self._start_ms = 0
                self._end_ms = dur
            else:
                self._start_ms, self._end_ms = self._clamp_range(old_range[0], old_range[1])
            self._position_ms = max(0, min(self._position_ms, dur))

        self.update()
        if (self._start_ms, self._end_ms) != old_range:
            self.rangeChanged.emit(self._start_ms, self._end_ms)

    def set_range(self, start_ms: int, end_ms: int) -> None:
        """Set the trim selection ``[start_ms, end_ms]``, clamping to valid bounds."""
        if self._duration_ms <= 0:
            return

        old_range = (self._start_ms, self._end_ms)
        clamped_start, clamped_end = self._clamp_range(int(start_ms), int(end_ms))
        self._start_ms = clamped_start
        self._end_ms = clamped_end
        self.update()

        if (clamped_start, clamped_end) != old_range:
            self.rangeChanged.emit(clamped_start, clamped_end)

    def set_position(self, position_ms: int) -> None:
        """Update playhead cursor position in milliseconds without emitting ``seekRequested``."""
        if self._duration_ms <= 0:
            clamped = 0
        else:
            clamped = max(0, min(int(position_ms), self._duration_ms))

        if clamped != self._position_ms:
            self._position_ms = clamped
            self.update()

    def track_rect(self) -> QRectF:
        """Return the drawable timeline track rectangle in widget coordinates."""
        left = float(self._margin_x)
        right = float(max(self._margin_x + 1, self.width() - self._margin_x))
        top = (self.height() * 0.56) - (self._track_height / 2.0)
        return QRectF(left, top, right - left, float(self._track_height))

    def _effective_min_span(self) -> int:
        if self._duration_ms <= 0:
            return 0
        return self._min_span_ms if self._duration_ms >= self._min_span_ms else 1

    def _clamp_range(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        if self._duration_ms <= 0:
            return (0, 0)

        min_span = self._effective_min_span()
        start = max(0, min(start_ms, self._duration_ms - min_span))
        end = max(min_span, min(end_ms, self._duration_ms))

        if end - start < min_span:
            if start + min_span <= self._duration_ms:
                end = start + min_span
            else:
                end = self._duration_ms
                start = max(0, end - min_span)
        return (start, end)

    def _ms_to_x(self, ms: int) -> float:
        rect = self.track_rect()
        if self._duration_ms <= 0:
            return rect.left()
        ratio = max(0.0, min(1.0, ms / self._duration_ms))
        return rect.left() + ratio * rect.width()

    def _x_to_ms(self, x: float) -> int:
        rect = self.track_rect()
        if self._duration_ms <= 0 or rect.width() <= 0:
            return 0
        ratio = max(0.0, min(1.0, (x - rect.left()) / rect.width()))
        return round(ratio * self._duration_ms)

    def _hit_test(self, pos: QPointF) -> _ActiveHandle | None:
        if self._duration_ms <= 0:
            return None

        rect = self.track_rect()
        x = pos.x()
        y = pos.y()

        sx = self._ms_to_x(self._start_ms)
        ex = self._ms_to_x(self._end_ms)
        px = self._ms_to_x(self._position_ms)

        d_start = abs(x - sx)
        d_end = abs(x - ex)
        d_play = abs(x - px)

        # Clicking the top playhead cap zone prioritizes the playhead handle.
        if y < rect.top() - 3 and d_play <= self._handle_hit_radius:
            return "playhead"

        start_hit = d_start <= self._handle_hit_radius
        end_hit = d_end <= self._handle_hit_radius
        if start_hit and end_hit:
            if sx == ex:
                return "start" if x < sx else "end"
            return "start" if x <= (sx + ex) / 2.0 else "end"
        if start_hit:
            return "start"
        if end_hit:
            return "end"
        if d_play <= 8:
            return "playhead"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start dragging a handle or jump playhead on track click."""
        if event.button() != Qt.MouseButton.LeftButton or self._duration_ms <= 0:
            super().mousePressEvent(event)
            return

        pos = event.position()
        hit = self._hit_test(pos)
        if hit is not None:
            self._active_handle = hit
            self._apply_drag(pos.x(), emit_playhead_unchanged=False)
        else:
            # Clicked empty track area: seek immediately and allow scrubbing.
            self._active_handle = "playhead"
            target_ms = self._x_to_ms(pos.x())
            self._position_ms = target_ms
            self.update()
            self.seekRequested.emit(target_ms)

        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Update active handle position or hover highlight."""
        pos = event.position()
        if self._active_handle is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self._apply_drag(pos.x(), emit_playhead_unchanged=False)
            event.accept()
            return

        hovered = self._hit_test(pos)
        if hovered != self._hovered_handle:
            self._hovered_handle = hovered
            self.setCursor(
                Qt.CursorShape.SizeHorCursor
                if hovered in ("start", "end", "playhead")
                else Qt.CursorShape.PointingHandCursor
                if self._duration_ms > 0
                else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finish handle drag interaction."""
        if event.button() == Qt.MouseButton.LeftButton and self._active_handle is not None:
            self._active_handle = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Clear hover state when cursor exits the widget."""
        if self._hovered_handle is not None:
            self._hovered_handle = None
            self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def _apply_drag(self, x: float, *, emit_playhead_unchanged: bool) -> None:
        if self._duration_ms <= 0 or self._active_handle is None:
            return

        target_ms = self._x_to_ms(x)
        min_span = self._effective_min_span()

        if self._active_handle == "start":
            new_start = max(0, min(target_ms, self._end_ms - min_span))
            if new_start != self._start_ms:
                self._start_ms = new_start
                self.update()
                self.rangeChanged.emit(self._start_ms, self._end_ms)
        elif self._active_handle == "end":
            new_end = min(self._duration_ms, max(target_ms, self._start_ms + min_span))
            if new_end != self._end_ms:
                self._end_ms = new_end
                self.update()
                self.rangeChanged.emit(self._start_ms, self._end_ms)
        elif self._active_handle == "playhead":
            new_pos = max(0, min(target_ms, self._duration_ms))
            changed = new_pos != self._position_ms
            self._position_ms = new_pos
            self.update()
            if changed or emit_playhead_unchanged:
                self.seekRequested.emit(self._position_ms)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Render timeline track, selected range, trim handles, and playhead cursor."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.track_rect()
        radius = rect.height() / 2.0

        # Base unselected track.
        painter.setPen(QPen(QColor("#363d4d"), 1.0))
        painter.setBrush(QColor("#232833"))
        painter.drawRoundedRect(rect, radius, radius)

        if self._duration_ms <= 0:
            painter.end()
            return

        sx = self._ms_to_x(self._start_ms)
        ex = self._ms_to_x(self._end_ms)
        px = self._ms_to_x(self._position_ms)

        # Selected range highlight bar.
        sel_width = max(2.0, ex - sx)
        sel_rect = QRectF(sx, rect.top(), sel_width, rect.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#3b82f6"))
        painter.drawRoundedRect(sel_rect, radius, radius)

        # Playhead vertical cursor line and top triangle indicator.
        playhead_color = QColor("#fbbf24")
        if self._active_handle == "playhead" or self._hovered_handle == "playhead":
            playhead_color = QColor("#fde68a")

        painter.setPen(QPen(playhead_color, 2.0))
        line_top = rect.top() - 7.0
        line_bottom = rect.bottom() + 7.0
        painter.drawLine(QPointF(px, line_top), QPointF(px, line_bottom))

        tri = QPainterPath()
        tri.moveTo(px - 5.5, line_top - 5.0)
        tri.lineTo(px + 5.5, line_top - 5.0)
        tri.lineTo(px, line_top + 1.5)
        tri.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(playhead_color)
        painter.drawPath(tri)

        # Trim handles (start and end).
        self._draw_trim_handle(
            painter,
            x=sx,
            center_y=rect.center().y(),
            active=self._active_handle == "start",
            hovered=self._hovered_handle == "start",
        )
        self._draw_trim_handle(
            painter,
            x=ex,
            center_y=rect.center().y(),
            active=self._active_handle == "end",
            hovered=self._hovered_handle == "end",
        )

        painter.end()

    def _draw_trim_handle(
        self,
        painter: QPainter,
        *,
        x: float,
        center_y: float,
        active: bool,
        hovered: bool,
    ) -> None:
        w = 12.0
        h = 24.0
        handle_rect = QRectF(x - w / 2.0, center_y - h / 2.0, w, h)

        fill = QColor("#ffffff") if (active or hovered) else QColor("#e2e8f0")
        border = QColor("#60a5fa") if active else QColor("#2563eb")
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(fill)
        painter.drawRoundedRect(handle_rect, 4.0, 4.0)

        # Center grip line for tactile affordance.
        painter.setPen(QPen(QColor("#475569"), 1.2))
        painter.drawLine(
            QPointF(x, center_y - 5.0),
            QPointF(x, center_y + 5.0),
        )
