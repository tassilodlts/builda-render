import io
import json
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

MAX_ANNOTATIONS = 3

MAX_AREA_FRAC = 0.60          # discard polygons covering >60% of image
MIN_POLYGON_POINTS = 4        # polygon is valid with 4 points
MIN_POLYLINE_POINTS = 2       # line is valid with 2 points (but we still prefer polygon)

DEFAULT_FALLBACK_BOX_FRAC = (0.25, 0.25, 0.75, 0.75)  # if nothing usable comes in


def safe_load_spec(spec: str) -> Dict[str, Any]:
    spec = (spec or "").strip()
    try:
        data = json.loads(spec) if spec else {}
        return data if isinstance(data, dict) else {"polylines": []}
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
    # Preferred format
    if isinstance(data.get("polylines"), list):
        return [s for s in data["polylines"] if isinstance(s, dict)]

    # Legacy single shape
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


def polygon_area(points: List[Tuple[int, int]]) -> float:
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


def bbox_area(b: Tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = b
    return max(0, x2 - x1) * max(0, y2 - y1)


def bbox_center_dist2(b: Tuple[int, int, int, int], cx: float, cy: float) -> float:
    x1, y1, x2, y2 = b
    mx = (x1 + x2) / 2.0
    my = (y1 + y2) / 2.0
    dx = mx - cx
    dy = my - cy
    return dx * dx + dy * dy


def get_text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        return draw.textsize(text, font=font)


def draw_rounded_polyline(
    base: Image.Image,
    pts: List[Tuple[int, int]],
    stroke_width: int,
    color_rgba: Tuple[int, int, int, int],
) -> None:
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

    # Product rule: cap at 3
    shapes = shapes[:MAX_ANNOTATIONS]

    # Font size relative to image size
    font_size = max(16, int(min(width, height) * 0.025))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    # Always yellow
    stroke_rgba = (255, 215, 0, 255)
    fill_alpha = clamp(int(style["fill_alpha"]), 0, 255)
    fill_rgba = (255, 215, 0, fill_alpha)

    stroke_width = max(2, int(style["stroke_width"]))
    smooth = bool(style["smooth"])
    smooth_iters = clamp(int(style["smooth_iters"]), 0, 6)

    # Layers (fill under stroke, labels on top)
    fill_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fill_draw = ImageDraw.Draw(fill_overlay)

    stroke_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    stroke_base = stroke_overlay  # alias for draw_rounded_polyline

    label_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(label_overlay)
    tmp_draw = ImageDraw.Draw(img)

    # Track candidates for fallback even if they fail strict checks
    fallback_candidates: List[Tuple[Tuple[int, int, int, int], str]] = []

    valid_drawn = 0

    for shape in shapes:
        if valid_drawn >= MAX_ANNOTATIONS:
            break

        label = str(shape.get("label", "Issue")).strip()[:40]
        stype = str(shape.get("type", "polygon")).lower().strip()
        unit = str(shape.get("unit", "norm")).lower().strip()

        if unit != "norm":
            continue

        raw_pts = shape.get("points", [])
        if not isinstance(raw_pts, list):
            continue

        pts_px: List[Tuple[int, int]] = []
        for p in raw_pts:
            if not isinstance(p, dict) or "x" not in p or "y" not in p:
                continue
            try:
                pts_px.append(norm_to_px(float(p["x"]), float(p["y"]), width, height))
            except Exception:
                continue

        # Prefer polygon, allow polyline only if explicitly requested
        if stype == "polyline":
            if len(pts_px) < MIN_POLYLINE_POINTS:
                continue
            closed = False
        else:
            stype = "polygon"
            if len(pts_px) < MIN_POLYGON_POINTS:
                continue
            closed = True

        # Close polygons
        if closed and pts_px[0] != pts_px[-1]:
            pts_px.append(pts_px[0])

        draw_pts = pts_px
        if smooth and len(pts_px) >= 4:
            draw_pts = chaikin_smooth(pts_px, iters=smooth_iters, closed=closed)

        # Save fallback candidate bbox early
        try:
            bb = bbox_of_points(draw_pts)
            fallback_candidates.append((bb, label))
        except Exception:
            pass

        # Safety checks
        if stype == "polygon":
            area_px2 = polygon_area(draw_pts)
            img_area = float(width * height)
            if img_area > 0 and (area_px2 / img_area) > MAX_AREA_FRAC:
                continue

            if len(draw_pts) >= 4:
                fill_draw.polygon(draw_pts, fill=fill_rgba)

        # Stroke always on top
        draw_rounded_polyline(
            base=stroke_base,
            pts=draw_pts,
            stroke_width=stroke_width,
            color_rgba=stroke_rgba,
        )

        # Label outside bbox, top-left
        bx1, by1, bx2, by2 = bbox_of_points(draw_pts)
        tw, th = get_text_size(tmp_draw, label, font)
        pad = max(6, int(font_size * 0.35))

        lx = bx1
        ly = by1 - (th + 2 * pad)
        if ly < 0:
            ly = by2 + pad

        lx = clamp(lx, 0, max(0, width - (tw + 2 * pad)))
        ly = clamp(ly, 0, max(0, height - (th + 2 * pad)))

        box = [lx, ly, lx + tw + 2 * pad, ly + th + 2 * pad]
        label_draw.rectangle(box, fill=(0, 0, 0, 180))
        label_draw.text((lx + pad, ly + pad), label, fill=(255, 255, 255, 255), font=font)

        valid_drawn += 1

    # Fallback: if nothing drawn, draw one generous bbox around best candidate
    if valid_drawn == 0:
        cx = width / 2.0
        cy = height / 2.0

        if fallback_candidates:
            # Choose most central candidate, tie-break by larger bbox
            best = None
            for bb, label in fallback_candidates:
                d2 = bbox_center_dist2(bb, cx, cy)
                a = bbox_area(bb)
                score = (d2, -a)
                if best is None or score < best[0]:
                    best = (score, bb, label)

            _, bb, label = best
            x1, y1, x2, y2 = bb

            # Make it more generous
