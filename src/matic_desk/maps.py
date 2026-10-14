"""Draw the floor plan PNG: rooms and robot (vendored renderer) plus the route."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from io import BytesIO

from PIL import Image, ImageDraw

from .vendor.hermes_client.floor_plan import render_floor_plan
from .vendor.hermes_client.models import FloorPlan, RobotPose

MAP_SIZE = 1024
ROUTE_COLOR = "#FFD166"
ROUTE_START_COLOR = "#06D6A0"


def _projector(plan: FloorPlan, width: int, height: int) -> Callable[[float, float], tuple[float, float]]:
    """Same projection the vendored renderer uses, so the route lines up with the rooms."""
    points = [point for room in plan.rooms for point in room.boundary]
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    padding = max(32, min(width, height) // 18)
    scale = min(
        (width - 2 * padding) / max(max_x - min_x, 0.1),
        (height - 2 * padding) / max(max_y - min_y, 0.1),
    )
    origin_x = (width - (max_x - min_x) * scale) / 2
    origin_y = (height - (max_y - min_y) * scale) / 2
    return lambda x, y: (origin_x + (x - min_x) * scale, height - origin_y - (y - min_y) * scale)


def render_map(
    plan: FloorPlan | None,
    pose: RobotPose | None,
    current_area: str | None,
    route: Sequence[tuple[float, float]] = (),
    size: int = MAP_SIZE,
) -> bytes:
    base = render_floor_plan(plan, pose, current_area, width=size, height=size)
    if plan is None or not plan.rooms or not route:
        return base
    image = Image.open(BytesIO(base)).convert("RGBA")
    project = _projector(plan, size, size)
    points = [project(x, y) for x, y in route]
    draw = ImageDraw.Draw(image)
    if len(points) > 1:
        draw.line(points, fill=ROUTE_COLOR, width=4, joint="curve")
    start_x, start_y = points[0]
    draw.ellipse((start_x - 6, start_y - 6, start_x + 6, start_y + 6), fill=ROUTE_START_COLOR)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
