"""Desktop interface for the SVG to DXF converter."""

from __future__ import annotations

import math
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPainterPath, QPen
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from converter import ConversionPreview, SvgToDxfConverter, relative_error_for_slider


class EndpointItem(QGraphicsItem):
    """Draw fitted segment endpoints with a constant on-screen size."""

    def __init__(self, points: list[QPointF]) -> None:
        super().__init__()
        self.points = points
        xs = [point.x() for point in points]
        ys = [point.y() for point in points]
        self.bounds = QRectF(
            min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        ).adjusted(-3.0, -3.0, 3.0, 3.0)
        self.setZValue(2.0)

    def boundingRect(self) -> QRectF:
        return self.bounds

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        pen = QPen(QColor("#ffcf66"))
        pen.setWidthF(4.0)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPoints(self.points)


class PreviewView(QGraphicsView):
    """Overlay fitted DXF geometry on a subdued rendering of the source SVG."""

    def __init__(self) -> None:
        super().__init__()
        self._source: Path | None = None
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#20242b"))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setMinimumHeight(330)

    def show_preview(self, source: Path, preview: ConversionPreview) -> None:
        resolved_source = source.resolve()
        preserve_view = self._source == resolved_source
        previous_transform = self.transform()
        previous_center = self.mapToScene(self.viewport().rect().center())

        scene = self.scene()
        scene.clear()

        original = QGraphicsSvgItem(str(source))
        original.setOpacity(0.22)
        scene.addItem(original)

        pen = QPen(QColor("#28d7ff"))
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        endpoints: list[QPointF] = []
        for chain in preview.chains:
            if not chain.primitives:
                continue
            first = chain.primitives[0].start
            path = QPainterPath(QPointF(first[0], -first[1]))
            for primitive in chain.primitives:
                endpoints.append(QPointF(primitive.start[0], -primitive.start[1]))
                if primitive.kind == "line" or primitive.center is None:
                    path.lineTo(primitive.end[0], -primitive.end[1])
                    continue
                start_angle = math.atan2(
                    primitive.start[1] - primitive.center[1],
                    primitive.start[0] - primitive.center[0],
                )
                steps = max(8, math.ceil(abs(primitive.sweep) / math.radians(5.0)))
                for index in range(1, steps + 1):
                    angle = start_angle + primitive.sweep * index / steps
                    x = primitive.center[0] + primitive.radius * math.cos(angle)
                    y = primitive.center[1] + primitive.radius * math.sin(angle)
                    path.lineTo(x, -y)
            if chain.closed:
                path.closeSubpath()
            else:
                last = chain.primitives[-1].end
                endpoints.append(QPointF(last[0], -last[1]))
            scene.addPath(path, pen)

        for center, radius in preview.circles:
            scene.addEllipse(
                center[0] - radius,
                -center[1] - radius,
                radius * 2.0,
                radius * 2.0,
                pen,
            )
        if endpoints:
            scene.addItem(EndpointItem(endpoints))

        bounds = scene.itemsBoundingRect()
        margin = max(bounds.width(), bounds.height()) * 0.04
        scene.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))
        if preserve_view:
            self.setTransform(previous_transform)
            self.centerOn(previous_center)
        else:
            self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._source = resolved_source

    def wheelEvent(self, event) -> None:
        pointer = event.position().toPoint()
        scene_under_pointer = self.mapToScene(pointer)
        factor = 1.18 if event.angleDelta().y() > 0 else 1.0 / 1.18
        self.scale(factor, factor)
        moved_scene_position = self.mapToScene(pointer)
        current_center = self.mapToScene(self.viewport().rect().center())
        self.centerOn(
            current_center
            + scene_under_pointer
            - moved_scene_position
        )
        event.accept()


class PreviewSignals(QObject):
    """Carry worker results safely back to the GUI thread."""

    completed = Signal(int, str, object, float)


class PreviewWorker(QRunnable):
    """Fit preview geometry without blocking window interaction."""

    def __init__(self, generation: int, source: Path, error_slider: int) -> None:
        super().__init__()
        self.generation = generation
        self.source = source
        self.error_slider = error_slider
        self.signals = PreviewSignals()

    @Slot()
    def run(self) -> None:
        started = time.perf_counter()
        try:
            message: object = SvgToDxfConverter(
                error_slider=self.error_slider
            ).preview(self.source)
        except Exception as error:
            message = error
        elapsed = time.perf_counter() - started
        self.signals.completed.emit(
            self.generation,
            str(self.source),
            message,
            elapsed,
        )


class MainWindow(QMainWindow):
    """Collect paths, explain fitting precision, and preview the real output."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SVG to DXF Converter")
        self.resize(920, 760)
        self.setAcceptDrops(True)
        self._preview_generation = 0
        self._slow_preview = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._start_preview)
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(2)
        self._build_ui()
        self._update_error_label()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        files = QGroupBox("Files")
        file_layout = QGridLayout(files)
        self.input_path = QLineEdit()
        self.output_path = QLineEdit()
        self.input_path.editingFinished.connect(self._queue_preview)
        input_button = QPushButton("Browse...")
        output_button = QPushButton("Browse...")
        input_button.clicked.connect(self._select_input)
        output_button.clicked.connect(self._select_output)
        file_layout.addWidget(QLabel("Input SVG:"), 0, 0)
        file_layout.addWidget(self.input_path, 0, 1)
        file_layout.addWidget(input_button, 0, 2)
        file_layout.addWidget(QLabel("Output DXF:"), 1, 0)
        file_layout.addWidget(self.output_path, 1, 1)
        file_layout.addWidget(output_button, 1, 2)
        layout.addWidget(files)

        fitting = QGroupBox("Local curve fitting error")
        fitting_layout = QGridLayout(fitting)
        self.error_slider = QSlider(Qt.Orientation.Horizontal)
        self.error_slider.setRange(0, 100)
        self.error_slider.setValue(30)
        self.error_slider.setTickInterval(10)
        self.error_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.error_slider.valueChanged.connect(self._error_changed)
        self.error_slider.sliderReleased.connect(self._refresh_preview_now)
        self.error_value = QLabel()
        self.error_value.setMinimumWidth(210)
        fitting_layout.addWidget(QLabel("Highest precision"), 0, 0)
        fitting_layout.addWidget(self.error_slider, 0, 1)
        fitting_layout.addWidget(QLabel("More simplification"), 0, 2)
        fitting_layout.addWidget(QLabel("Allowed curve error:"), 1, 0)
        fitting_layout.addWidget(self.error_value, 1, 1, 1, 2)
        layout.addWidget(fitting)

        explanation = QLabel(
            "The logarithmic slider sets the maximum local fitting error from "
            "0.001% to 50% of each curved feature's size. Fitting minimizes "
            "polyline vertices within that limit, then prefers lower error. "
            "Native straight lines remain exact. The preview overlays fitted "
            "DXF geometry in cyan and its endpoints in amber on the source SVG."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        preview_group = QGroupBox("Output preview")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_view = PreviewView()
        preview_layout.addWidget(self.preview_view)
        self.preview_stats = QLabel("Select an SVG to calculate a preview.")
        self.preview_stats.setWordWrap(True)
        preview_layout.addWidget(self.preview_stats)
        layout.addWidget(preview_group, 1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.convert_button = QPushButton("Convert SVG to DXF")
        self.convert_button.setMinimumSize(220, 44)
        self.convert_button.clicked.connect(self._convert)
        button_row.addWidget(self.convert_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        self.status = QLabel("Ready")
        layout.addWidget(self.status)

    def _select_input(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select SVG",
            "",
            "SVG files (*.svg);;All files (*.*)",
        )
        if not filename:
            return
        self._set_input(Path(filename))

    def _set_input(self, source: Path) -> None:
        """Select one SVG and derive its default output path."""

        self._slow_preview = False
        self.input_path.setText(str(source))
        self.output_path.setText(str(source.with_suffix(".dxf")))
        self._queue_preview()

    @staticmethod
    def _dropped_svg(event: QDragEnterEvent | QDropEvent) -> Path | None:
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            source = Path(url.toLocalFile())
            if source.is_file() and source.suffix.lower() == ".svg":
                return source
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._dropped_svg(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        source = self._dropped_svg(event)
        if source is None:
            event.ignore()
            return
        self._set_input(source)
        event.acceptProposedAction()

    def _select_output(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save DXF",
            self.output_path.text(),
            "DXF files (*.dxf)",
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.lower() != ".dxf":
            destination = destination.with_suffix(".dxf")
        self.output_path.setText(str(destination))

    def _error_changed(self) -> None:
        self._update_error_label()
        if self._slow_preview and self.error_slider.isSliderDown():
            self._preview_generation += 1
            self.preview_stats.setText("Release the slider to update this complex SVG.")
            return
        self._queue_preview()

    def _update_error_label(self) -> None:
        percentage = relative_error_for_slider(self.error_slider.value()) * 100.0
        self.error_value.setText(f"{percentage:.4g}% of each curved feature")

    def _queue_preview(self) -> None:
        self._preview_generation += 1
        if Path(self.input_path.text().strip()).is_file():
            self.preview_stats.setText("Waiting to update preview...")
            self._preview_timer.start()

    def _refresh_preview_now(self) -> None:
        self._preview_timer.stop()
        self._start_preview()

    def _start_preview(self) -> None:
        source = Path(self.input_path.text().strip())
        if not source.is_file():
            return
        generation = self._preview_generation
        self.preview_stats.setText("Calculating local curve fits...")
        worker = PreviewWorker(generation, source, self.error_slider.value())
        worker.signals.completed.connect(self._preview_ready)
        self._thread_pool.start(worker)

    @Slot(int, str, object, float)
    def _preview_ready(
        self,
        generation: int,
        source_text: str,
        outcome: object,
        elapsed: float,
    ) -> None:
        if generation != self._preview_generation:
            return
        if isinstance(outcome, Exception):
            self.preview_stats.setText(f"Preview failed: {outcome}")
            return
        preview = outcome
        self.preview_view.show_preview(Path(source_text), preview)
        result = preview.result
        self._slow_preview = elapsed >= 1.0
        self.preview_stats.setText(
            f"Allowed curve error: {result.relative_error_percent:.4g}% | "
            f"Local tolerance: {result.minimum_tolerance:.6g} to "
            f"{result.maximum_tolerance:.6g} coordinate units | "
            f"Observed relative error: {result.observed_relative_error_percent:.4g}% | "
            f"{result.vertices} polyline vertices | "
            f"{result.circles + result.polylines} DXF entities "
            f"({result.circles} circles, {result.polylines} polylines), "
            f"{result.arcs} arc segments, {result.lines} line segments, "
            f"{elapsed * 1000.0:.0f} ms"
        )

    def _convert(self) -> None:
        source = Path(self.input_path.text().strip())
        destination_text = self.output_path.text().strip()
        if not source.is_file():
            QMessageBox.warning(self, "Input", "Select a valid SVG file.")
            return
        if not destination_text:
            QMessageBox.warning(self, "Output", "Select an output DXF file.")
            return

        destination = Path(destination_text)
        self.convert_button.setEnabled(False)
        self.status.setText("Converting...")
        QApplication.processEvents()
        try:
            result = SvgToDxfConverter(
                error_slider=self.error_slider.value()
            ).convert(
                source,
                destination,
            )
            self.status.setText(
                f"Done - {result.circles} circles, {result.polylines} polylines, "
                f"{result.arcs} arcs, {result.lines} lines"
            )
            message = (
                "DXF created successfully.\n\n"
                f"{destination}\n\n"
                f"Circles: {result.circles}\n"
                f"Polylines: {result.polylines}\n"
                f"Arc segments: {result.arcs}\n"
                f"Line segments: {result.lines}\n"
                f"DXF vertices: {result.vertices}\n"
                f"Allowed curve error: {result.relative_error_percent:.4g}%\n"
                f"Local tolerance: {result.minimum_tolerance:.6g} to "
                f"{result.maximum_tolerance:.6g} coordinate units\n"
                f"Observed relative error: "
                f"{result.observed_relative_error_percent:.4g}%\n"
                f"Maximum sampled deviation: {result.maximum_error:.6g} "
                "coordinate units"
            )
            if result.skipped:
                message += "\n\nSkipped SVG elements: " + ", ".join(result.skipped)
            QMessageBox.information(self, "Conversion complete", message)
        except Exception as error:
            self.status.setText("Conversion failed")
            QMessageBox.critical(self, "Conversion failed", str(error))
        finally:
            self.convert_button.setEnabled(True)


def main() -> int:
    """Start the desktop application."""

    application = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return application.exec()
