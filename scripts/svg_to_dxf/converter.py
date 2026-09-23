"""Convert native SVG geometry into compact DXF line and arc chains."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

import ezdxf
from svgpathtools import Line, parse_path


Point = tuple[float, float]
Matrix = tuple[float, float, float, float, float, float]
PointFunction = Callable[[float], Point]

EPSILON = 1e-12
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
TRANSFORM = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")


@dataclass(frozen=True)
class Primitive:
    """One DXF-compatible line or circular arc."""

    kind: str
    start: Point
    end: Point
    center: Point | None = None
    radius: float = 0.0
    sweep: float = 0.0
    error: float = 0.0


@dataclass(frozen=True)
class ConversionResult:
    """Summary returned after a successful conversion."""

    polylines: int
    vertices: int
    lines: int
    arcs: int
    circles: int
    skipped: tuple[str, ...]
    minimum_tolerance: float
    maximum_tolerance: float
    maximum_error: float
    relative_error_percent: float
    observed_relative_error_percent: float


@dataclass(frozen=True)
class PreviewChain:
    """One fitted path exposed to the desktop preview."""

    primitives: tuple[Primitive, ...]
    closed: bool


@dataclass(frozen=True)
class ConversionPreview:
    """Fitted geometry and statistics without writing a DXF file."""

    result: ConversionResult
    chains: tuple[PreviewChain, ...]
    circles: tuple[tuple[Point, float], ...]


def number(value: str | None, default: float = 0.0) -> float:
    """Read the first SVG number while tolerating unit suffixes."""

    match = NUMBER.search(value or "")
    return float(match.group()) if match else default


def point_list(value: str | None) -> list[Point]:
    values = [float(item) for item in NUMBER.findall(value or "")]
    return list(zip(values[0::2], values[1::2]))


def distance(first: Point, second: Point) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def same_point(first: Point, second: Point, tolerance: float = 1e-10) -> bool:
    return distance(first, second) <= tolerance


def line_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= EPSILON:
        return distance(point, start)
    return abs((point[0] - start[0]) * dy - (point[1] - start[1]) * dx) / length


def matrix_multiply(left: Matrix, right: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def transform_point(matrix: Matrix, point: Point) -> Point:
    a, b, c, d, e, f = matrix
    x, y = point
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(value: str | None) -> Matrix:
    result = IDENTITY
    for name, arguments in TRANSFORM.findall(value or ""):
        values = [float(item) for item in NUMBER.findall(arguments)]
        operation = IDENTITY
        key = name.lower()
        if key == "matrix" and len(values) >= 6:
            operation = (
                values[0],
                values[1],
                values[2],
                values[3],
                values[4],
                values[5],
            )
        elif key == "translate" and values:
            operation = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) > 1 else 0.0)
        elif key == "scale" and values:
            sy = values[1] if len(values) > 1 else values[0]
            operation = (values[0], 0.0, 0.0, sy, 0.0, 0.0)
        elif key == "rotate" and values:
            angle = math.radians(values[0])
            cosine = math.cos(angle)
            sine = math.sin(angle)
            rotation = (cosine, sine, -sine, cosine, 0.0, 0.0)
            if len(values) >= 3:
                cx, cy = values[1:3]
                operation = matrix_multiply(
                    matrix_multiply((1.0, 0.0, 0.0, 1.0, cx, cy), rotation),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                operation = rotation
        elif key == "skewx" and values:
            operation = (1.0, 0.0, math.tan(math.radians(values[0])), 1.0, 0.0, 0.0)
        elif key == "skewy" and values:
            operation = (1.0, math.tan(math.radians(values[0])), 0.0, 1.0, 0.0, 0.0)
        result = matrix_multiply(result, operation)
    return result


def circumcircle(first: Point, middle: Point, last: Point) -> tuple[Point, float] | None:
    ax, ay = first
    bx, by = middle
    cx, cy = last
    denominator = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    scale = max(distance(first, middle), distance(middle, last), distance(first, last), 1.0)
    if abs(denominator) < EPSILON * scale * scale:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    center = (
        (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / denominator,
        (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / denominator,
    )
    return center, distance(center, first)


def signed_angle(first: Point, second: Point) -> float:
    cross = first[0] * second[1] - first[1] * second[0]
    dot = first[0] * second[0] + first[1] * second[1]
    return math.atan2(cross, dot)


def append_unique(target: list[Point], source: Iterable[Point]) -> None:
    for point in source:
        if not target or not same_point(target[-1], point, 1e-12):
            target.append(point)


def points_diagonal(points: Iterable[Point]) -> float:
    """Return the bounding-box diagonal for one local geometric feature."""

    materialized = list(points)
    if not materialized:
        return 0.0
    xs = [point[0] for point in materialized]
    ys = [point[1] for point in materialized]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def relative_error_for_slider(value: int) -> float:
    """Map slider steps logarithmically from 0.001 to 50 percent."""

    clamped = min(100, max(0, int(value)))
    return 1e-5 * math.pow(50000.0, clamped / 100.0)


def local_curve_tolerance(point_at: PointFunction, relative_error: float) -> float:
    """Derive tolerance from a curve itself instead of the whole drawing."""

    probes = [point_at(index / 32.0) for index in range(33)]
    scale = max(points_diagonal(probes), 1e-9)
    return max(scale * relative_error, 1e-12)


def native_curve_samples(point_at: PointFunction, tolerance: float) -> list[Point]:
    """Evaluate an SVG curve exactly; samples validate fits and are never exported."""

    points: list[Point] = []
    sampling_error = max(tolerance * 0.2, 1e-9)

    def visit(start_t: float, end_t: float, depth: int) -> None:
        start = point_at(start_t)
        end = point_at(end_t)
        quarter_t = start_t + (end_t - start_t) * 0.25
        middle_t = start_t + (end_t - start_t) * 0.5
        three_quarter_t = start_t + (end_t - start_t) * 0.75
        interior = [point_at(quarter_t), point_at(middle_t), point_at(three_quarter_t)]
        deviation = max(line_distance(point, start, end) for point in interior)
        if deviation > sampling_error and depth < 12:
            visit(start_t, middle_t, depth + 1)
            visit(middle_t, end_t, depth + 1)
            return
        append_unique(points, [start, *interior, end])

    for section in range(4):
        visit(section / 4.0, (section + 1) / 4.0, 0)
    return points


def straight_samples(start: Point, end: Point) -> list[Point]:
    return [
        (
            start[0] + (end[0] - start[0]) * index / 4.0,
            start[1] + (end[1] - start[1]) * index / 4.0,
        )
        for index in range(5)
    ]


def line_fit(points: list[Point], start_index: int, end_index: int, tolerance: float):
    start = points[start_index]
    end = points[end_index]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= EPSILON:
        return None

    maximum_error = 0.0
    previous_projection = -1e-9
    for point in points[start_index:end_index + 1]:
        projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
        if projection < previous_projection - 1e-7 or projection > 1.0 + 1e-7:
            return None
        previous_projection = projection
        maximum_error = max(maximum_error, line_distance(point, start, end))
        if maximum_error > tolerance:
            return None
    return Primitive("line", start, end, error=maximum_error), maximum_error


def arc_fit(
    points: list[Point],
    start_index: int,
    end_index: int,
    tolerance: float,
):
    start = points[start_index]
    end = points[end_index]
    if same_point(start, end):
        return None
    anchor_index = max(
        range(start_index + 1, end_index),
        key=lambda index: line_distance(points[index], start, end),
        default=-1,
    )
    if anchor_index < 0:
        return None
    circle = circumcircle(start, points[anchor_index], end)
    if circle is None:
        return None
    center, radius = circle
    if not math.isfinite(radius) or radius <= EPSILON:
        return None

    maximum_error = 0.0
    increments: list[float] = []
    previous = (start[0] - center[0], start[1] - center[1])
    for point in points[start_index + 1:end_index + 1]:
        vector = (point[0] - center[0], point[1] - center[1])
        increment = signed_angle(previous, vector)
        if abs(increment) > 1e-10:
            increments.append(increment)
        previous = vector
        maximum_error = max(maximum_error, abs(distance(point, center) - radius))
        if maximum_error > tolerance:
            return None
    if not increments:
        return None
    if not (all(value > 0.0 for value in increments) or all(value < 0.0 for value in increments)):
        return None
    sweep = sum(increments)
    if abs(sweep) < math.radians(0.01) or abs(sweep) > math.pi + 1e-9:
        return None
    return Primitive("arc", start, end, center, radius, sweep, maximum_error), maximum_error


def minimum_primitive_spans(
    points: list[Point],
    tolerance: float,
) -> list[tuple[int, int, Primitive]]:
    """Minimize valid primitives over error-derived native curve boundaries."""

    if len(points) < 2:
        return []
    final = len(points) - 1
    boundaries = {0, final}

    def split_invalid(start_index: int, end_index: int) -> None:
        if end_index - start_index <= 1:
            return
        line = line_fit(points, start_index, end_index, tolerance)
        arc = arc_fit(points, start_index, end_index, tolerance)
        if line is not None or arc is not None:
            return
        middle = (start_index + end_index) // 2
        boundaries.add(middle)
        split_invalid(start_index, middle)
        split_invalid(middle, end_index)

    split_invalid(0, final)
    knots = sorted(boundaries)
    scores: list[tuple[int, float] | None] = [None] * len(knots)
    choices: list[tuple[int, Primitive] | None] = [None] * len(knots)
    scores[-1] = (0, 0.0)

    for knot_index in range(len(knots) - 2, -1, -1):
        start_index = knots[knot_index]
        best_key = None
        best_choice = None
        for end_knot in range(knot_index + 1, len(knots)):
            end_index = knots[end_knot]
            tail = scores[end_knot]
            if tail is None:
                continue
            candidates = [line_fit(points, start_index, end_index, tolerance)]
            if end_index - start_index >= 2:
                candidates.append(arc_fit(points, start_index, end_index, tolerance))
            for candidate in candidates:
                if candidate is None:
                    continue
                primitive, error = candidate
                key = (
                    tail[0] + 1,
                    tail[1] + error / max(tolerance, EPSILON),
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best_choice = (end_knot, primitive)
        scores[knot_index] = best_key
        choices[knot_index] = best_choice

    primitives: list[tuple[int, int, Primitive]] = []
    knot_index = 0
    while knot_index < len(knots) - 1:
        start_index = knots[knot_index]
        choice = choices[knot_index]
        if choice is None:
            end_index = knots[knot_index + 1]
            primitives.append(
                (
                    start_index,
                    end_index,
                    Primitive("line", points[start_index], points[end_index]),
                )
            )
            knot_index += 1
        else:
            next_knot, primitive = choice
            primitives.append((start_index, knots[next_knot], primitive))
            knot_index = next_knot
    return primitives


def minimum_primitives(points: list[Point], tolerance: float) -> list[Primitive]:
    return [
        primitive
        for _start, _end, primitive in minimum_primitive_spans(points, tolerance)
    ]


def merge_collinear_lines(primitives: list[Primitive], closed: bool) -> list[Primitive]:
    """Remove vertices between consecutive lines on the same straight path."""

    def merged(first: Primitive, second: Primitive) -> Primitive | None:
        if first.kind != "line" or second.kind != "line":
            return None
        if not same_point(first.end, second.start, 1e-9):
            return None
        first_dx = first.end[0] - first.start[0]
        first_dy = first.end[1] - first.start[1]
        second_dx = second.end[0] - second.start[0]
        second_dy = second.end[1] - second.start[1]
        if first_dx * second_dx + first_dy * second_dy <= 0.0:
            return None
        length = distance(first.start, second.end)
        if line_distance(first.end, first.start, second.end) > max(length, 1.0) * 1e-10:
            return None
        return Primitive("line", first.start, second.end, error=max(first.error, second.error))

    compact: list[Primitive] = []
    for primitive in primitives:
        replacement = merged(compact[-1], primitive) if compact else None
        if replacement is None:
            compact.append(primitive)
        else:
            compact[-1] = replacement
    if closed and len(compact) > 1:
        replacement = merged(compact[-1], compact[0])
        if replacement is not None:
            compact = [*compact[1:-1], replacement]
    return compact


def closed_circle(points: list[Point], tolerance: float) -> tuple[Point, float, float] | None:
    """Recognize a complete circle independently from fitting preferences."""

    ring = points[:-1] if len(points) > 1 and same_point(points[0], points[-1]) else points
    if len(ring) < 8:
        return None
    circle = circumcircle(ring[0], ring[len(ring) // 3], ring[2 * len(ring) // 3])
    if circle is None:
        return None
    center, radius = circle
    radial_limit = max(tolerance, radius * 1e-9, 1e-10)
    radial_error = max(abs(distance(point, center) - radius) for point in ring)
    if radial_error > radial_limit:
        return None

    vectors = [(point[0] - center[0], point[1] - center[1]) for point in [*ring, ring[0]]]
    increments = [
        signed_angle(vectors[index], vectors[index + 1])
        for index in range(len(vectors) - 1)
    ]
    if not (all(value > 0.0 for value in increments) or all(value < 0.0 for value in increments)):
        return None
    if abs(abs(sum(increments)) - 2.0 * math.pi) > 1e-4:
        return None
    return center, radius, radial_error


class SvgToDxfConverter:
    """Read SVG elements and emit optimized DXF lightweight polylines."""

    def __init__(
        self,
        error_slider: int = 30,
    ) -> None:
        self.error_slider = min(100, max(0, int(error_slider)))
        self.relative_error = relative_error_for_slider(self.error_slider)
        self.document = None
        self.modelspace = None
        self.polylines = 0
        self.vertices = 0
        self.lines = 0
        self.arcs = 0
        self.circles = 0
        self.skipped: set[str] = set()
        self.tolerances: list[float] = []
        self.maximum_error = 0.0
        self.maximum_relative_error = 0.0
        self.preview_chains: list[PreviewChain] = []
        self.preview_circles: list[tuple[Point, float]] = []

    def convert(self, source: str | Path, destination: str | Path) -> ConversionResult:
        result = self._build(source)
        self.document.saveas(destination)
        return result

    def preview(self, source: str | Path) -> ConversionPreview:
        """Fit a document for display without writing an intermediate file."""

        result = self._build(source)
        return ConversionPreview(
            result,
            tuple(self.preview_chains),
            tuple(self.preview_circles),
        )

    def _build(self, source: str | Path) -> ConversionResult:
        root = ET.parse(source).getroot()
        self.document = ezdxf.new("R2010")
        self.document.units = ezdxf.units.MM
        self.modelspace = self.document.modelspace()
        self.polylines = self.vertices = self.lines = self.arcs = self.circles = 0
        self.skipped.clear()
        self.tolerances.clear()
        self.maximum_error = 0.0
        self.maximum_relative_error = 0.0
        self.preview_chains.clear()
        self.preview_circles.clear()

        self._walk(root, (1.0, 0.0, 0.0, -1.0, 0.0, 0.0))
        return ConversionResult(
            self.polylines,
            self.vertices,
            self.lines,
            self.arcs,
            self.circles,
            tuple(sorted(self.skipped)),
            min(self.tolerances, default=0.0),
            max(self.tolerances, default=0.0),
            self.maximum_error,
            self.relative_error * 100.0,
            self.maximum_relative_error * 100.0,
        )

    def _add_chain(self, primitives: list[Primitive], closed: bool) -> None:
        primitives = merge_collinear_lines(primitives, closed)
        if not primitives:
            return
        vertices = [
            (
                item.start[0],
                item.start[1],
                math.tan(item.sweep / 4.0) if item.kind == "arc" else 0.0,
            )
            for item in primitives
        ]
        if not closed:
            end = primitives[-1].end
            vertices.append((end[0], end[1], 0.0))
        self.modelspace.add_lwpolyline(vertices, format="xyb", close=closed)
        self.polylines += 1
        self.vertices += len(vertices)
        self.lines += sum(item.kind == "line" for item in primitives)
        self.arcs += sum(item.kind == "arc" for item in primitives)
        self.maximum_error = max(
            self.maximum_error,
            max((item.error for item in primitives), default=0.0),
        )
        self.preview_chains.append(PreviewChain(tuple(primitives), closed))

    def _add_circle(self, center: Point, radius: float, error: float = 0.0) -> None:
        self.modelspace.add_circle(center, radius)
        self.circles += 1
        self.preview_circles.append((center, radius))
        self.maximum_error = max(self.maximum_error, error)

    def _record_tolerance(self, tolerance: float) -> float:
        self.tolerances.append(tolerance)
        return tolerance

    def _record_curve_error(
        self, primitives: list[Primitive], tolerance: float
    ) -> None:
        if primitives and self.relative_error > 0.0:
            scale = tolerance / self.relative_error
            self.maximum_relative_error = max(
                self.maximum_relative_error,
                max(item.error for item in primitives) / scale,
            )

    def _path(self, element: ET.Element, matrix: Matrix) -> None:
        data = element.get("d")
        if not data:
            return
        path = parse_path(data)
        for subpath in path.continuous_subpaths():
            segments = [
                segment
                for segment in subpath
                if not isinstance(segment, Line) or abs(segment.end - segment.start) > 1e-10
            ]
            prepared: list[tuple[object, PointFunction, float]] = []
            for segment in segments:
                def point_at(value: float, current=segment) -> Point:
                    point = current.point(value)
                    return transform_point(matrix, (point.real, point.imag))

                if isinstance(segment, Line):
                    prepared.append((segment, point_at, 0.0))
                else:
                    tolerance = self._record_tolerance(
                        local_curve_tolerance(point_at, self.relative_error)
                    )
                    prepared.append((segment, point_at, tolerance))

            if prepared and all(
                not isinstance(item[0], Line) for item in prepared
            ):
                ring: list[Point] = []
                circle_tolerance = min(item[2] for item in prepared)
                for _segment, point_at, tolerance in prepared:
                    append_unique(ring, native_curve_samples(point_at, tolerance))
                recognized = closed_circle(ring, circle_tolerance)
                if recognized is not None:
                    self._add_circle(*recognized)
                    self.maximum_relative_error = max(
                        self.maximum_relative_error,
                        recognized[2] / (circle_tolerance / self.relative_error),
                    )
                    continue

            primitives: list[Primitive] = []
            curved_points: list[Point] = []
            curved_tolerances: list[float] = []

            def flush_curves() -> None:
                if not curved_points:
                    return
                tolerance = min(curved_tolerances)
                fitted = minimum_primitives(curved_points, tolerance)
                self._record_curve_error(fitted, tolerance)
                primitives.extend(fitted)
                curved_points.clear()
                curved_tolerances.clear()

            for segment, point_at, tolerance in prepared:
                if isinstance(segment, Line):
                    flush_curves()
                    start = point_at(0.0)
                    end = point_at(1.0)
                    if not same_point(start, end):
                        primitives.append(Primitive("line", start, end))
                else:
                    samples = native_curve_samples(point_at, tolerance)
                    if curved_points and len(curved_points) + len(samples) > 128:
                        flush_curves()
                    append_unique(curved_points, samples)
                    curved_tolerances.append(tolerance)
            flush_curves()
            subpath_tolerances = [
                tolerance
                for _segment, _point_at, tolerance in prepared
                if tolerance > 0.0
            ]
            closed = (
                len(primitives) > 1
                and same_point(
                    primitives[0].start,
                    primitives[-1].end,
                    max(subpath_tolerances, default=1e-7) * 1e-3,
                )
            )
            self._add_chain(primitives, closed)

    def _line(self, element: ET.Element, matrix: Matrix) -> None:
        start = transform_point(matrix, (number(element.get("x1")), number(element.get("y1"))))
        end = transform_point(matrix, (number(element.get("x2")), number(element.get("y2"))))
        if not same_point(start, end):
            self._add_chain([Primitive("line", start, end)], False)

    def _polyline(self, element: ET.Element, matrix: Matrix, closed: bool) -> None:
        source = [transform_point(matrix, point) for point in point_list(element.get("points"))]
        if len(source) < 2:
            return
        if closed:
            source.append(source[0])
        primitives = [
            Primitive("line", start, end)
            for start, end in zip(source, source[1:])
            if not same_point(start, end)
        ]
        self._add_chain(primitives, closed)

    def _rect(self, element: ET.Element, matrix: Matrix) -> None:
        x = number(element.get("x"))
        y = number(element.get("y"))
        width = number(element.get("width"))
        height = number(element.get("height"))
        if width <= 0.0 or height <= 0.0:
            return
        rx = number(element.get("rx"))
        ry = number(element.get("ry"))
        if rx <= 0.0 and ry <= 0.0:
            corners = [
                transform_point(matrix, point)
                for point in [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
            ]
            primitives = [
                Primitive("line", corners[index], corners[(index + 1) % 4])
                for index in range(4)
            ]
            self._add_chain(primitives, True)
            return
        rx = min(rx if rx > 0.0 else ry, width / 2.0)
        ry = min(ry if ry > 0.0 else rx, height / 2.0)
        sections = [
            ((x + rx, y), (x + width - rx, y), None),
            ((x + width - rx, y + ry), None, (-math.pi / 2.0, 0.0)),
            ((x + width, y + ry), (x + width, y + height - ry), None),
            ((x + width - rx, y + height - ry), None, (0.0, math.pi / 2.0)),
            ((x + width - rx, y + height), (x + rx, y + height), None),
            ((x + rx, y + height - ry), None, (math.pi / 2.0, math.pi)),
            ((x, y + height - ry), (x, y + ry), None),
            ((x + rx, y + ry), None, (math.pi, 3.0 * math.pi / 2.0)),
        ]
        primitives: list[Primitive] = []
        for center_or_start, end, angles in sections:
            if end is not None:
                start = transform_point(matrix, center_or_start)
                finish = transform_point(matrix, end)
                primitives.append(
                    Primitive("line", start, finish)
                )
                continue
            center = center_or_start
            start_angle, end_angle = angles

            def point_at(value: float) -> Point:
                angle = start_angle + (end_angle - start_angle) * value
                native = (center[0] + rx * math.cos(angle), center[1] + ry * math.sin(angle))
                return transform_point(matrix, native)

            tolerance = self._record_tolerance(
                local_curve_tolerance(point_at, self.relative_error)
            )
            fitted = minimum_primitives(
                native_curve_samples(point_at, tolerance),
                tolerance,
            )
            self._record_curve_error(fitted, tolerance)
            primitives.extend(fitted)
        self._add_chain(primitives, True)

    def _ellipse(self, element: ET.Element, matrix: Matrix, circle: bool) -> None:
        cx = number(element.get("cx"))
        cy = number(element.get("cy"))
        rx = number(element.get("r") if circle else element.get("rx"))
        ry = rx if circle else number(element.get("ry"))
        if rx <= 0.0 or ry <= 0.0:
            return
        points: list[Point] = []
        tolerances: list[float] = []
        for section in range(4):
            start_angle = section * math.pi / 2.0
            end_angle = (section + 1) * math.pi / 2.0

            def point_at(value: float, start=start_angle, end=end_angle) -> Point:
                angle = start + (end - start) * value
                native = (cx + rx * math.cos(angle), cy + ry * math.sin(angle))
                return transform_point(matrix, native)

            tolerance = self._record_tolerance(
                local_curve_tolerance(point_at, self.relative_error)
            )
            tolerances.append(tolerance)
            section_points = native_curve_samples(point_at, tolerance)
            append_unique(points, section_points)
        append_unique(points, [points[0]])
        tolerance = min(tolerances)
        recognized = closed_circle(points, tolerance)
        if recognized is not None:
            self._add_circle(*recognized)
            self.maximum_relative_error = max(
                self.maximum_relative_error,
                recognized[2] / (tolerance / self.relative_error),
            )
        else:
            fitted = minimum_primitives(points, tolerance)
            self._record_curve_error(fitted, tolerance)
            self._add_chain(fitted, True)

    def _walk(self, element: ET.Element, parent_matrix: Matrix) -> None:
        tag = element.tag.split("}")[-1].lower()
        style = element.get("style", "").replace(" ", "").lower()
        if (
            element.get("display", "").lower() == "none"
            or element.get("visibility", "").lower() == "hidden"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            return
        if tag in {"defs", "clippath", "mask", "pattern", "marker", "symbol"}:
            return

        matrix = matrix_multiply(parent_matrix, parse_transform(element.get("transform")))
        if tag == "path":
            self._path(element, matrix)
        elif tag == "line":
            self._line(element, matrix)
        elif tag == "polyline":
            self._polyline(element, matrix, False)
        elif tag == "polygon":
            self._polyline(element, matrix, True)
        elif tag == "rect":
            self._rect(element, matrix)
        elif tag == "circle":
            self._ellipse(element, matrix, True)
        elif tag == "ellipse":
            self._ellipse(element, matrix, False)
        elif tag in {"text", "image", "use"}:
            self.skipped.add(tag)

        for child in element:
            self._walk(child, matrix)
