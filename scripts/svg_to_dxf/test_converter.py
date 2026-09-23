"""Regression tests for local SVG curve fitting."""

from pathlib import Path
import tempfile
import unittest

from converter import (
    SvgToDxfConverter,
    local_curve_tolerance,
    minimum_primitives,
    relative_error_for_slider,
)


class LocalToleranceTests(unittest.TestCase):
    def test_tolerance_uses_curve_scale(self) -> None:
        tolerance = local_curve_tolerance(
            lambda value: (value, value * value), relative_error_for_slider(45)
        )
        expected = 2.0 ** 0.5 * relative_error_for_slider(45)
        self.assertAlmostEqual(tolerance, expected)

    def test_large_viewbox_does_not_dilute_small_curve(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10000 10000">'
            '<path d="M 1 1 C 1 2 2 2 2 1"/>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "local-curve.svg"
            source.write_text(svg, encoding="utf-8")
            result = SvgToDxfConverter(error_slider=0).preview(source).result

        self.assertGreater(result.maximum_tolerance, 0.0)
        self.assertLess(result.maximum_tolerance, 0.001)

    def test_exact_circle_remains_a_circle(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<circle cx="50" cy="50" r="2"/>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "circle.svg"
            source.write_text(svg, encoding="utf-8")
            result = SvgToDxfConverter(error_slider=0).preview(source).result

        self.assertEqual(result.circles, 1)
        self.assertEqual(result.arcs, 0)

    def test_logarithmic_error_endpoints(self) -> None:
        self.assertAlmostEqual(relative_error_for_slider(0), 0.00001)
        self.assertAlmostEqual(relative_error_for_slider(50), 50000.0 ** 0.5 * 1e-5)
        self.assertAlmostEqual(relative_error_for_slider(100), 0.5)

    def test_equal_segment_count_favors_more_exact_arc(self) -> None:
        points = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
        precise = minimum_primitives(points, 1.0)

        self.assertEqual([item.kind for item in precise], ["arc"])
        self.assertAlmostEqual(precise[0].error, 0.0)

    def test_adjacent_svg_curves_fit_as_one_arc(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10 0 A 10 10 0 0 1 0 10 '
            'A 10 10 0 0 1 -10 0"/>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-quarter-arcs.svg"
            source.write_text(svg, encoding="utf-8")
            preview = SvgToDxfConverter(error_slider=100).preview(source)

        self.assertEqual(preview.result.arcs, 1)
        self.assertEqual(preview.result.lines, 0)
        self.assertEqual(preview.result.vertices, 2)
        self.assertLess(preview.result.observed_relative_error_percent, 1e-8)

    def test_collinear_native_lines_do_not_add_vertices(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 0 0 L 1 0 L 2 0 L 3 0 L 3 1"/>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "collinear.svg"
            source.write_text(svg, encoding="utf-8")
            preview = SvgToDxfConverter(error_slider=100).preview(source)

        self.assertEqual(preview.result.lines, 2)
        self.assertEqual(preview.result.vertices, 3)
        self.assertEqual(preview.result.polylines, 1)

    def test_higher_error_reduces_curve_complexity(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
            '<path d="M 1 10 C 1 1 19 1 19 10 C 19 19 1 19 1 10"/>'
            '</svg>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "curve.svg"
            source.write_text(svg, encoding="utf-8")
            results = [
                SvgToDxfConverter(error_slider=value).preview(source).result
                for value in (0, 50, 100)
            ]

        counts = [result.lines + result.arcs + result.circles for result in results]
        self.assertGreater(counts[0], counts[-1])
        self.assertLessEqual(counts[-1], counts[1])
        self.assertLessEqual(counts[1], counts[0])
        for result in results:
            self.assertLessEqual(
                result.observed_relative_error_percent,
                result.relative_error_percent + 1e-9,
            )


if __name__ == "__main__":
    unittest.main()
