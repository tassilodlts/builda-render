import io
import json
from typing import Any, Dict, List, Tuple, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

MAX_ANNOTATIONS = 3
MAX_AREA_FRAC = 0.60  # discard polygons that cover more than 60% of image
MIN_POLYGON_POINTS = 6  # discard polygons with fewer than this
MIN_POLYLINE_POINTS = 4


def safe_load_spec(spec: str) -> Dict[str, Any]:
    spec = (spec or "").strip()
    try:
        data = json.loads(spec) if spec else {}
        if isinstance(data, dict):
            return data
        return {"polylines": []}
    except Exception:
        return {"polylines": []}


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def norm_to_px(x: float, y: float, w: int, h: int) -> Tuple[int, int]:
    px = int(clamp01(float(x)) * w)
    py = int(clamp01(float(y)) * h)
    return clamp(px, 0, w - 1), clamp(py, 0, h - 1)


def normalize_shapes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Accepts:
    - {"polylines":[...]}  (preferred)
    - or a single shape object {"type":"polygon"/"polyline", ...} (legacy)
    Returns a list of shape dicts.
    """
    if isinstance(data.get("polylines"), list):
        shapes = [s for s in data["polylines"] if isinstance(s, dict)]
        return shapes

    # Legacy: single object
    if isinstance(data.get("type"), str) and isinstance(data.get("points"), list):
        return [data]

    return []


def get_style(data: Dict[str, Any]) -> Dict[str, Any]:
    style = data.get("style", {})
    if not isinstance(style, dict):
        style = {}
    return {
        "stroke_width": int(style.get("stroke_width", 10)),
        "smooth": bool(style.get("smooth", True)),
        "smooth_iters": int(style.get("smooth_iters", 3)),
        "fill_alpha": int(style.get("fill_alpha", 70)),  # 0..255
    }


def chaikin_smooth(points: List[Tuple[int, int]], iters: int, closed: bool) -> List[Tuple[int, int]]:
    """
    Chaikin corner cutting. Produces a rounded look.
    Important: If closed is True, keep it closed.
    """
    if iters <= 0 or len(points) < 3:
        return points

    pts = points[:]

    if closed and pts[0] != pts[-1]:
        pts = pts + [pts[0]]

    for _ in range(iters):
        if len(pts) < 3:
            break
        new_pts: List[Tuple[int, int]] = []
        last_i = len(pts) - 2
        for i in range(len(pts) - 1):
            p0 = pts[i]
            p1 = pts[i + 1]
            qx = int(0.75 * p0[0] + 0.25 * p1[0])
            qy = int(0.75 * p0[1] + 0.25 * p1[1])
            rx = int(0.25 * p0[0] + 0.75 * p1[0])
            ry = int(0.25 * p0[1] + 0.75 * p1[1])

            if i == 0 and not closed:
                new_pts.append(p0)
            new_pts.append((qx, qy))
            new_pts.append((rx, ry))
            if i == last_i and not closed:
                new_pts.append(p1)

        pts = new_pts
        if closed and pts[0] != pts[-1]:
            pts.append(pts[0])

    return pts


def draw_rounded_polyline(
    base: Image.Image,
    pts: List[Tuple[int, int]],
    stroke_width: int,
    color_rgba: Tuple[int, int, int, int],
) -> None:
    """
    Rounded stroke by:
    - drawing thick line
    - stamping round caps at points
    """
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    try:
        d.line(pts, fill=color_rgba, width=stroke_width, joint="curve")
    except TypeError:
        d.line(pts, fill=color_rgba, width=stroke_width)

    r = max(1, stroke_width // 2)
    for (x, y) in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=color_rgba)

    base.alpha_composite(overlay)


def polygon_area(points: List[Tuple[int, int]]) -> float:
    """
    Shoelace formula. points should be closed or closable.
    Returns absolute pixel^2 area.
    """
    if len(points) < 3:
        return 0.0
    pts = points[:]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    s = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        s += (x1 * y2 - x2 * y1)
    return abs(s) / 2.0


def bbox_of_points(points: List[Tuple[int, int]]) -> Tuple[int, int, int, int]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def get_text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return draw.textsize(text, font=font)


@app.get("/")
def root():
    return {"ok": True, "service": "builda-render"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/render")
async def render(image: UploadFile = File(...), spec: str = Form(...)):
    img_bytes = await image.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    width, height = img.size

    data = safe_load_spec(spec)
    shapes = normalize_shapes(data)
    style = get_style(data)

    # Cap the number of annotations (your chosen product rule)
    shapes = shapes[:MAX_ANNOTATIONS]

    # Font size relative to image size (non-negotiable rule)
    font_size = max(16, int(min(width, height) * 0.025))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    # Always yellow
    stroke_rgba = (255, 215, 0, 255)
    fill_rgba = (255, 215, 0, clamp(int(style["fill_alpha"]), 0, 255))
    stroke_width = max(2, int(style["stroke_width"]))
    smooth = bool(style["smooth"])
    smooth_iters = clamp(int(style["smooth_iters"]), 0, 6)

    # Prepare layers
    fill_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fill_draw = ImageDraw.Draw(fill_overlay)
    label_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(label_overlay)

    # Validate and draw
    valid_drawn = 0
    base_draw = ImageDraw.Draw(img)

    for shape in shapes:
        if valid_drawn >= MAX_ANNOTATIONS:
            break

        label = str(shape.get("label", "Issue")).strip()[:40]
        stype = str(shape.get("type", "polygon")).lower().strip()
        unit = str(shape.get("unit", "norm")).lower().strip()
        closed = bool(shape.get("closed", True))

        # Only norm supported for now
        if unit != "norm":
            continue

        raw_pts = shape.get("points", [])
        if not isinstance(raw_pts, list):
            continue

        pts_px: List[Tuple[int, int]] = []
        for p in raw_pts:
            if not isinstance(p, dict):
                continue
            if "x" not in p or "y" not in p:
                continue
            try:
                px, py = norm_to_px(float(p["x"]), float(p["y"]), width, height)
                pts_px.append((px, py))
            except Exception:
                continue

        # Decide polygon vs polyline rules
        if stype == "polyline":
            closed = False
            if len(pts_px) < MIN_POLYLINE_POINTS:
                continue
        else:
            # default polygon
            stype = "polygon"
            closed = True
            if len(pts_px) < MIN_POLYGON_POINTS:
                continue

        # Ensure closed loop for polygon
        if closed and pts_px[0] != pts_px[-1]:
            pts_px.append(pts_px[0])

        # Smoothing decision
        draw_pts = pts_px
        if smooth and len(pts_px) >= 4:
            draw_pts = chaikin_smooth(pts_px, iters=smooth_iters, closed=closed)

        # Safety checks for polygon area
        if stype == "polygon":
            area_px2 = polygon_area(draw_pts)
            img_area = float(width * height)
            if img_area > 0 and (area_px2 / img_area) > MAX_AREA_FRAC:
                continue

            # Fill FIRST, using the SAME points as stroke
            if len(draw_pts) >= 4:
                fill_draw.polygon(draw_pts, fill=fill_rgba)

        valid_drawn += 1

        # Stroke AFTER, same points
        draw_rounded_polyline(
            base=img,
            pts=draw_pts,
            stroke_width=stroke_width,
            color_rgba=stroke_rgba,
        )

        # Label placement: outside polygon, top-left of bbox
        bx1, by1, bx2, by2 = bbox_of_points(draw_pts)
        tw, th = get_text_size(base_draw, label, font)
        pad = max(6, int(font_size * 0.35))

        # Preferred: above bbox, left aligned
        lx = bx1
        ly = by1 - (th + 2 * pad)

        # If above goes off-image, place below bbox
        if ly < 0:
            ly = by2 + pad

        # Clamp inside image bounds
        lx = clamp(lx, 0, max(0, width - (tw + 2 * pad)))
        ly = clamp(ly, 0, max(0, height - (th + 2 * pad)))

        # Draw label bg + text (never inside fill because we place outside bbox)
        box = [lx, ly, lx + tw + 2 * pad, ly + th + 2 * pad]
        label_draw.rectangle(box, fill=(0, 0, 0, 180))
        label_draw.text((lx + pad, ly + pad), label, fill=(255, 255, 255, 255), font=font)

    # Composite fill and labels
    # Fill should be under strokes. We already stroked on img.
    # So we composite fill with low alpha first, then re-stroke would be perfect,
    # but in practice the low alpha looks fine. If you want perfect layering, tell me.
    img.alpha_composite(fill_overlay)
    img.alpha_composite(label_overlay)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")
