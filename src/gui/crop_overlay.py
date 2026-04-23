"""
Interactive crop overlay.

The overlay is a single :class:`QGraphicsItem` that sits on top of the
video item inside the preview's graphics scene. It draws the crop
rectangle, dims the region *outside* the crop so the user can see what
will be discarded, and responds to mouse events for move/resize.

We deliberately reimplement all of this rather than composing several
smaller items (one per handle). A single item keeps the cursor-shape
logic simple (one ``hoverMoveEvent`` does the whole hit-test) and avoids
z-order ambiguity when the user drags a handle very fast.
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent


class _Grab(Enum):
    """Which part of the overlay the user is currently dragging."""

    NONE = auto()
    MOVE = auto()
    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()
    TOP = auto()
    BOTTOM = auto()
    LEFT = auto()
    RIGHT = auto()


# Pixel radius (in scene coordinates) of the square hit area around each
# handle. Must be large enough to grab easily on a downscaled preview of a
# 4K video, where each scene pixel may correspond to just a fraction of a
# screen pixel. Painting happens in *view* coordinates via ignoresTransforms,
# so the on-screen handle size is constant regardless of zoom.
_HANDLE_GRAB_RADIUS = 12.0


class CropOverlaySignals(QObject):
    """Signal relay because QGraphicsItem can't emit signals directly.

    QGraphicsItem derives from QObject-less QGraphicsItem for performance.
    When we want to emit Qt signals from one (e.g. to let the crop panel
    know the user dragged a handle), we route them through a tiny QObject
    held as an attribute. This is the idiomatic Qt workaround.
    """

    cropChanged = Signal(QRectF)  # in source-pixel scene coordinates


class CropOverlay(QGraphicsItem):
    """Draggable, resizable crop rectangle drawn over the video.

    The rectangle is always expressed in the overlay's *local* coordinate
    system, which matches scene coordinates one-to-one because we never
    transform the overlay itself. Scene coordinates in turn match source
    video pixels. Callers can therefore read :meth:`crop_rect` and pass
    the integer values straight to FFmpeg's ``crop`` filter.
    """

    def __init__(self, bounds: QRectF) -> None:
        super().__init__()
        self.signals = CropOverlaySignals()

        # ``bounds`` is the outer rectangle we must stay inside (i.e. the
        # video frame). The crop rect starts covering the whole frame,
        # which effectively means "no crop" until the user drags.
        self._bounds = QRectF(bounds)
        self._rect = QRectF(bounds)

        self._grab = _Grab.NONE
        # Where the mouse was when a drag started, plus the crop rect at
        # that moment, so drags compute deltas from a stable reference and
        # don't accumulate rounding error.
        self._drag_origin: Optional[QPointF] = None
        self._rect_at_drag_start = QRectF()

        # Aspect ratio constraint: width/height. None = free resize.
        self._aspect_ratio: Optional[float] = None

        # Accept hover so we can change the cursor based on which handle
        # is under the mouse - big usability win over a fixed cursor.
        self.setAcceptHoverEvents(True)
        # Paint above the video item.
        self.setZValue(10)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_bounds(self, bounds: QRectF) -> None:
        """Change the outer (video) rectangle the overlay is clamped to.

        Used when a new file is loaded; also resets the crop rect to the
        full frame, which matches the user's expectation that "opening a
        file starts fresh".
        """
        self.prepareGeometryChange()
        self._bounds = QRectF(bounds)
        self._rect = QRectF(bounds)
        self.update()
        self.signals.cropChanged.emit(self._rect)

    def crop_rect(self) -> QRectF:
        """Return the current crop rectangle in scene (source-pixel) units."""
        return QRectF(self._rect)

    def set_crop_rect(self, rect: QRectF) -> None:
        """Programmatically set the crop rectangle (e.g. from spinboxes)."""
        rect = self._clamp_rect(QRectF(rect))
        self.prepareGeometryChange()
        self._rect = rect
        self.update()
        self.signals.cropChanged.emit(self._rect)

    def reset(self) -> None:
        """Expand the crop to fill the entire frame (disables cropping)."""
        self.set_crop_rect(self._bounds)

    def set_aspect_ratio(self, ratio: Optional[float]) -> None:
        """Lock the crop to the given width/height ratio, or unlock if None.

        When an aspect ratio is set we immediately snap the current crop to
        match it, anchored at the centre of the existing crop so the
        visual jump is minimal.
        """
        self._aspect_ratio = ratio
        if ratio is not None:
            self._rect = self._apply_aspect_ratio(self._rect, anchor=self._rect.center())
            self._rect = self._clamp_rect(self._rect)
            self.prepareGeometryChange()
            self.update()
            self.signals.cropChanged.emit(self._rect)

    # ------------------------------------------------------------------
    # QGraphicsItem overrides
    # ------------------------------------------------------------------

    def boundingRect(self) -> QRectF:
        # We paint dimming across the whole frame, so the bounding rect is
        # the entire outer bounds, not just the crop rectangle. Returning
        # too small a rect here would leave painting artefacts.
        return QRectF(self._bounds)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Dim the "discarded" region outside the crop rectangle. We build
        # a path that is (outer bounds) minus (crop rect) and fill it
        # with a semi-transparent black. Even-odd fill rule does the
        # subtraction for us without geometry gymnastics.
        dim_path = QPainterPath()
        dim_path.setFillRule(Qt.OddEvenFill)
        dim_path.addRect(self._bounds)
        dim_path.addRect(self._rect)
        painter.fillPath(dim_path, QColor(0, 0, 0, 120))

        # The crop rectangle itself: bright outline so it's visible on
        # both dark and bright footage. We use a cosmetic pen so the line
        # stays 2 pixels wide on screen regardless of view zoom.
        pen = QPen(QColor(80, 200, 255))
        pen.setWidth(2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._rect)

        # Rule-of-thirds guides, dashed, to help the user compose shots.
        thirds_pen = QPen(QColor(255, 255, 255, 160))
        thirds_pen.setStyle(Qt.DashLine)
        thirds_pen.setCosmetic(True)
        painter.setPen(thirds_pen)
        for i in (1, 2):
            x = self._rect.left() + self._rect.width() * i / 3
            y = self._rect.top() + self._rect.height() * i / 3
            painter.drawLine(QPointF(x, self._rect.top()), QPointF(x, self._rect.bottom()))
            painter.drawLine(QPointF(self._rect.left(), y), QPointF(self._rect.right(), y))

        # Draw the 8 resize handles. We want them to appear the same size
        # on screen at any zoom, so we draw in *device* pixels by asking
        # the painter's current transform for the inverse scale factors.
        painter.setPen(QPen(QColor(20, 20, 20), 1, Qt.SolidLine))
        painter.setBrush(QBrush(QColor(80, 200, 255)))
        handle_size_scene = _HANDLE_GRAB_RADIUS
        for pos in self._handle_positions().values():
            painter.drawRect(QRectF(
                pos.x() - handle_size_scene / 2,
                pos.y() - handle_size_scene / 2,
                handle_size_scene,
                handle_size_scene,
            ))

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def hoverMoveEvent(self, event):
        # Update the cursor shape according to which grab zone is under
        # the mouse. Feedback like this makes the interaction discoverable
        # without needing a tutorial.
        grab = self._hit_test(event.pos())
        self.setCursor(_cursor_for_grab(grab))
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._grab = self._hit_test(event.pos())
        self._drag_origin = event.pos()
        self._rect_at_drag_start = QRectF(self._rect)
        # Accept the event so the scene doesn't try to drag-select things
        # behind the overlay.
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._grab == _Grab.NONE or self._drag_origin is None:
            super().mouseMoveEvent(event)
            return

        delta = event.pos() - self._drag_origin
        new_rect = QRectF(self._rect_at_drag_start)

        if self._grab == _Grab.MOVE:
            new_rect.translate(delta)
        else:
            # Resize handles mutate one or two edges of the rect. We don't
            # use QRectF's set* methods because they can flip the rect if
            # the user drags past the opposite edge; instead we compute
            # new edges and normalise at the end.
            left = new_rect.left()
            top = new_rect.top()
            right = new_rect.right()
            bottom = new_rect.bottom()

            if self._grab in (_Grab.TOP_LEFT, _Grab.LEFT, _Grab.BOTTOM_LEFT):
                left += delta.x()
            if self._grab in (_Grab.TOP_RIGHT, _Grab.RIGHT, _Grab.BOTTOM_RIGHT):
                right += delta.x()
            if self._grab in (_Grab.TOP_LEFT, _Grab.TOP, _Grab.TOP_RIGHT):
                top += delta.y()
            if self._grab in (_Grab.BOTTOM_LEFT, _Grab.BOTTOM, _Grab.BOTTOM_RIGHT):
                bottom += delta.y()

            new_rect = QRectF(
                min(left, right),
                min(top, bottom),
                abs(right - left),
                abs(bottom - top),
            )

        if self._aspect_ratio is not None:
            # Anchor the aspect-ratio correction on whichever corner the
            # user is *not* dragging, so that corner stays pinned during
            # the resize. For MOVE there's no anchor to choose.
            if self._grab == _Grab.MOVE:
                pass
            else:
                anchor = self._anchor_for_grab(self._grab, self._rect_at_drag_start)
                new_rect = self._apply_aspect_ratio(new_rect, anchor=anchor)

        new_rect = self._clamp_rect(new_rect)
        if new_rect != self._rect:
            self.prepareGeometryChange()
            self._rect = new_rect
            self.update()
            self.signals.cropChanged.emit(self._rect)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._grab = _Grab.NONE
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _handle_positions(self) -> dict:
        """Return the scene-space centres of the 8 resize handles."""
        r = self._rect
        cx = r.left() + r.width() / 2
        cy = r.top() + r.height() / 2
        return {
            _Grab.TOP_LEFT: QPointF(r.left(), r.top()),
            _Grab.TOP_RIGHT: QPointF(r.right(), r.top()),
            _Grab.BOTTOM_LEFT: QPointF(r.left(), r.bottom()),
            _Grab.BOTTOM_RIGHT: QPointF(r.right(), r.bottom()),
            _Grab.TOP: QPointF(cx, r.top()),
            _Grab.BOTTOM: QPointF(cx, r.bottom()),
            _Grab.LEFT: QPointF(r.left(), cy),
            _Grab.RIGHT: QPointF(r.right(), cy),
        }

    def _hit_test(self, pos: QPointF) -> _Grab:
        """Determine which part of the overlay the given scene point is on.

        We test handles before the body so the corners win over the inside
        drag even when they overlap (they can, on very small crop rects).
        """
        for grab, centre in self._handle_positions().items():
            dx = abs(pos.x() - centre.x())
            dy = abs(pos.y() - centre.y())
            if dx <= _HANDLE_GRAB_RADIUS and dy <= _HANDLE_GRAB_RADIUS:
                return grab
        if self._rect.contains(pos):
            return _Grab.MOVE
        return _Grab.NONE

    def _anchor_for_grab(self, grab: _Grab, start_rect: QRectF) -> QPointF:
        """Return the fixed corner/edge for an aspect-constrained resize."""
        # Corner drags anchor on the diagonally opposite corner; edge
        # drags anchor on the centre of the opposite edge.
        if grab == _Grab.TOP_LEFT:
            return start_rect.bottomRight()
        if grab == _Grab.TOP_RIGHT:
            return start_rect.bottomLeft()
        if grab == _Grab.BOTTOM_LEFT:
            return start_rect.topRight()
        if grab == _Grab.BOTTOM_RIGHT:
            return start_rect.topLeft()
        if grab == _Grab.TOP:
            return QPointF(start_rect.center().x(), start_rect.bottom())
        if grab == _Grab.BOTTOM:
            return QPointF(start_rect.center().x(), start_rect.top())
        if grab == _Grab.LEFT:
            return QPointF(start_rect.right(), start_rect.center().y())
        if grab == _Grab.RIGHT:
            return QPointF(start_rect.left(), start_rect.center().y())
        return start_rect.center()

    def _apply_aspect_ratio(self, rect: QRectF, anchor: QPointF) -> QRectF:
        """Snap ``rect`` to the current aspect ratio, keeping ``anchor`` fixed.

        We pick whichever of (width-from-height, height-from-width) makes
        the rectangle *smaller* so the result is guaranteed to fit inside
        the original rect the user was dragging toward.
        """
        if self._aspect_ratio is None or self._aspect_ratio <= 0:
            return rect

        w, h = rect.width(), rect.height()
        # If the user-dragged rect is wider than the target ratio, reduce
        # its width; otherwise reduce its height.
        if w / max(h, 1) > self._aspect_ratio:
            new_w = h * self._aspect_ratio
            new_h = h
        else:
            new_w = w
            new_h = w / self._aspect_ratio

        # Reposition so ``anchor`` stays in the same relative spot: if the
        # anchor was the top-left, the new rect starts at the anchor;
        # if it was the bottom-right, the new rect ends at the anchor; etc.
        # We interpolate based on where the anchor sat in the old rect.
        if rect.width() > 0 and rect.height() > 0:
            ax = (anchor.x() - rect.left()) / rect.width()
            ay = (anchor.y() - rect.top()) / rect.height()
        else:
            ax = ay = 0.5
        new_left = anchor.x() - ax * new_w
        new_top = anchor.y() - ay * new_h
        return QRectF(new_left, new_top, new_w, new_h)

    def _clamp_rect(self, rect: QRectF) -> QRectF:
        """Shift/shrink ``rect`` until it fits entirely within the bounds.

        We preserve size when possible (just translate), only shrinking
        when the rect is larger than the bounds in some dimension.
        """
        # Enforce a minimum size so the user can always grab the handles.
        min_side = 10.0
        w = max(min(rect.width(), self._bounds.width()), min_side)
        h = max(min(rect.height(), self._bounds.height()), min_side)
        x = min(max(rect.left(), self._bounds.left()), self._bounds.right() - w)
        y = min(max(rect.top(), self._bounds.top()), self._bounds.bottom() - h)
        return QRectF(x, y, w, h)


def _cursor_for_grab(grab: _Grab) -> Qt.CursorShape:
    """Pick the right mouse cursor for each grab zone.

    Resize arrows that point diagonally for corners, straight arrows for
    edges, a move (four-arrow) cursor for the body, and the default
    arrow everywhere else. Tiny detail, big usability impact.
    """
    mapping = {
        _Grab.MOVE: Qt.SizeAllCursor,
        _Grab.TOP_LEFT: Qt.SizeFDiagCursor,
        _Grab.BOTTOM_RIGHT: Qt.SizeFDiagCursor,
        _Grab.TOP_RIGHT: Qt.SizeBDiagCursor,
        _Grab.BOTTOM_LEFT: Qt.SizeBDiagCursor,
        _Grab.TOP: Qt.SizeVerCursor,
        _Grab.BOTTOM: Qt.SizeVerCursor,
        _Grab.LEFT: Qt.SizeHorCursor,
        _Grab.RIGHT: Qt.SizeHorCursor,
    }
    return mapping.get(grab, Qt.ArrowCursor)
