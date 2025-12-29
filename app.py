import io
import json
from typing import Any, Dict, List, Union, Tuple, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()


def safe_load_spec(spec: str) -> Dict[str, Any]:
    spec = (spec or "").strip()
    try:
        data = json.loads(spec) if spec else {}
        if isinstance(data, dict):
            return data
        return {"problem": str(data)}
    except Exception:
        return {"problem": spec}


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def norm_to_px(x: float, y: float, w: int, h: int) -> Tuple[int, int]:
    # x,y expected 0..1
    px = int(float(x) * w)
    py = int(float(y) * h)
    return clamp(px, 0, w - 1), clamp(py, 0, h - 1)


def normalize_polylines(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    polylines = data.get("polylines", [])
    if not isinstance(polylines, list):
        return []
    out = []
    for pl in polylines:
        if isinstance(pl, dict) and isinstance(pl.get("points"), list):
            out.append(pl)
    return out


def get_style(data: Dict[str, Any]) -> Dict[str, Any]:
    style = data.get("style", {})
    if not isinstance(style, dict):
        style = {}
    # Safe defaults
    return {
        "stroke_width": int(style.get("stroke_width", 8)),
        "smooth": bool(style.get("smooth", True)),
        "smooth_iters": int(style.get("smooth_iters", 3)),
        "fill_alpha": int(style.get("fill_alpha", 70)),  # 0..255
    }


def chaikin_smooth(points: List[Tuple[int, int]], iters: int, closed: bool) -> List[Tuple[int, int]]:
    """
    Chaikin corner cutting: turns sharp corners into smooth curves.
    Works well for "rounded polyline" look.
    """
    if iters <= 0 or len(points) < 3:
        return points

    pts = points[:]

    # Ensure closed has the first point repeated at end for processing
    if closed:
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]

    for _ in range(iters):
        if len(pts) < 3:
            break
        new_pts: List[Tuple[int, int]] = []
        rng = range(len(pts) - 1)
        for i in rng:
            p0 = pts[i]
            p1 = pts[i + 1]
            # Q = 0.75 p0 + 0.25 p1
            qx = int(0.75 * p0[0] + 0.25 * p1[0])
            qy = int(0.75 * p0[1] + 0.25 * p1[1])
            # R = 0.25 p0 + 0.75 p1
            rx = int(0.25 * p0[0] + 0.75 * p1[0])
            ry = int(0.25 * p0[1] + 0.75 * p1[1])
            if i == 0 and not closed:
                new_pts.append(p0)
            new_pts.append((qx, qy))
            new_pts.append((rx, ry))
            if i == len(pts) - 2 and not closed:
                new_pts.append(p1)

        pts = new_pts

        if closed:
            # Re-close
            if pts[0] != pts[-1]:
                pts.append(pts[0])

    return pts


def draw_rounded_polyline(
    base: Image.Image,
    pts: List[Tuple[int, int]],
    stroke_width: int,
    color_rgba: Tuple[int, int, int, int],
    closed: bool,
) -> None:
    """
    Draw a rounded looking polyline by:
    - drawing a thick line
    - adding circles along points to enforce roundness
    """
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Pillow supports joint="curve" on many versions. If not, fallback.
    try:
        d.line(pts, fill=color_rgba, width=stroke_width, joint="curve")
    except TypeError:
        d.line(pts, fill=color_rgba, width=stroke_width)

    # Add round caps / round joins explicitly by stamping circles
    r = max(1, stroke_width // 2)
    for (x, y) in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=color_rgba)

    # For closed shapes, also make sure the closing segment looks rounded
    if closed and len(pts) >= 2:
        (x0, y0) = pts[0]
        d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=color_rgba)

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
    polylines = normalize_polylines(data)
    style = get_style(data)

    # Font
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except Exception:
        font = ImageFont.load_default()

    # If nothing to draw, return original image
    if not polylines:
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    # Colors (yellow-ish stroke, semi transparent fill)
    stroke_rgba = (255, 215, 0, 255)  # solid
    fill_rgba = (255, 215, 0, clamp(style["fill_alpha"], 0, 255))  # transparent fill
    stroke_width = max(2, int(style["stroke_width"]))
    smooth = bool(style["smooth"])
    smooth_iters = clamp(int(style["smooth_iters"]), 0, 6)

    # We draw fills on overlay, and strokes on top, to look clean
    fill_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fill_draw = ImageDraw.Draw(fill_overlay)

    for pl in polylines:
        label = str(pl.get("label", "Issue")).strip()[:40]
        closed = bool(pl.get("closed", True))
        pts_raw = pl.get("points", [])
        if not isinstance(pts_raw, list) or len(pts_raw) < 2:
            continue

        # Convert points (norm) to pixels
        pts_px: List[Tuple[int, int]] = []
        for p in pts_raw:
            if not isinstance(p, dict):
                continue
            x = p.get("x", None)
            y = p.get("y", None)
            if x is None or y is None:
                continue
            try:
                px, py = norm_to_px(float(x), float(y), width, height)
                pts_px.append((px, py))
            except Exception:
                continue

        if len(pts_px) < 2:
            continue

        # Ensure closed loop if requested
        if closed and pts_px[0] != pts_px[-1]:
            pts_px.append(pts_px[0])

        # Fill: only if closed and enough points
        if closed and len(pts_px) >= 4:
            # Fill uses original points (not smoothed) to avoid strange self intersections
            fill_draw.polygon(pts_px, fill=fill_rgba)

        # Stroke: smoothed
        stroke_pts = pts_px
        if smooth and len(pts_px) >= 4:
            stroke_pts = chaikin_smooth(pts_px, iters=smooth_iters, closed=closed)

        draw_rounded_polyline(
            base=img,
            pts=stroke_pts,
            stroke_width=stroke_width,
            color_rgba=stroke_rgba,
            closed=closed,
        )

        # Label placement: use first point, clamp inside image
        lx, ly = pts_px[0]
        label_x = clamp(lx + 10, 0, width - 1)
        label_y = clamp(ly - 30, 0, height - 1)

        # Label background box
        # Get text size in a compatible way
        tmp_draw = ImageDraw.Draw(img)
        try:
            bbox = tmp_draw.textbbox((0, 0), label, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw, th = tmp_draw.textsize(label, font=font)

        pad = 6
        box = [label_x - pad, label_y - pad, label_x + tw + pad, label_y + th + pad]

        # Draw label box on overlay so it's clean
        label_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(label_overlay)
        ld.rectangle(box, fill=(0, 0, 0, 180))
        ld.text((label_x, label_y), label, fill=(255, 255, 255, 255), font=font)
        img.alpha_composite(label_overlay)

    # Apply fill overlay after drawing strokes? Better under strokes:
    # So composite fill first, then redraw strokes would be ideal.
    # But we already drew strokes on img. We'll composite fill now, low alpha, acceptable.
    img.alpha_composite(fill_overlay)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")
