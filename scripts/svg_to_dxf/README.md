# SVG to DXF

Run `run.py` directly in VS Code or from the repository root:

```sh
uv run scripts/svg_to_dxf/run.py
```

The interface controls the allowed local fitting error for non-straight SVG
curves. The logarithmic slider spans 0.001% to 50% of each curved feature's
bounding-box diagonal. Above 10%, fitting increasingly favors a valid straight
line over a circular arc when both use the same number of DXF objects. Native
straight lines remain exact. The same slider
position gives small and large curved features the same relative precision;
the overall SVG view box does not determine their error budget.

Select an SVG with the file picker or drop it anywhere on the window. The
interface calculates a debounced preview in a background worker. Cyan DXF
geometry is overlaid on a subdued rendering of the source SVG. The summary
reports the allowed local tolerance range, observed relative error, entity
counts, vertex count, and preview time. Drag to pan and
use the mouse wheel to zoom around the pointer.

The converter evaluates native SVG parametric geometry. Internal evaluations are
used only to validate error; they are not exported as an interpolated polyline.
It fits LINE and circular ARC candidates using each curve's local tolerance.
Circle recognition uses that tolerance instead of an unrelated percentage of
the radius. DXF arcs are stored as lightweight polyline bulges.
