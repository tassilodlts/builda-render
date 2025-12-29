import io
import json
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

MAX_ANNOTATIONS = 3
MAX_AREA_RATIO = 0.60  # 60% of image


# ----------------------------
# Helpers
# ----------------------------
def safe_load_spec(spec: str) -> Dict[str, Any]:
    try:
        data = json.loads(spec)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def norm_to_px(x: float, y: float, w: int, h: int) -> Tuple[int, int]:
    return (
        clamp(int(x * w), 0, w - 1),
        clamp(int(y * h), 0, h - 1),
    )


def polygon_area(pts: List[Tuple[int, int]]) -> float:
    area = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        area += (x1 * y2 - x2 * y1)
    return abs(area) / 2.0


def chaikin_smooth(pts: List[Tuple[int, int]], iters: int) -> List[Tuple[int, int]]:
    for _ in range(iters):
        new = []
        for i in range(len(pts) - 1):
            p0 = pts[i]
            p1 = pts[i + 1]
            q = (int(0.75*p0[0] + 0.25*p1[0]), int(0.75*p0[1] + 0.25*p1[1]))
            r = (int(0.25*p0[0] + 0.75*p1[0]), int(0.25*p0[1] + 0.75*p1[1]))
            if i == 0:
                new.append(p0)
            new.extend([q, r])
        new.append(pts[-1])
        pts = new
    return pts


# ----------------------------
# Routes
# ----------------------------
@app.post("/render")
async def render(image: UploadFile = File(...), spec: str = Form(...)):
    img = Image.open(io.BytesIO(await image.read())).convert("RGBA")
    w, h = img.size

    data = safe_load_spec(spec)
    shapes = data.get("polylines", [])
    shapes = [s for s in shapes if isinstance(s, dict)][:MAX_ANNOTATIONS]

    # STEP 3 — font size relative to image
    font_size = max(16, int(min(w, h) * 0.025))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    fill_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    stroke_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    label_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))

    fd = ImageDraw.Draw(fill_overlay)
    sd = ImageDraw.Draw(stroke_overlay)
    ld = ImageDraw.Draw(label_overlay)

    valid_shapes = []

    for s in shapes:
        pts_raw = s.get("points", [])
        if len(pts_raw) < 6:
            continue

        pts = []
        for p in pts_raw:
            if "x" in p and "y" in p:
                pts.append(norm_to_px(p["x"], p["y"], w, h))

        if len(pts) < 6:
            continue

        if pts[0] != pts[-1]:
            pts.append(pts[0])

        area = polygon_area(pts)
        if area > (w * h * MAX_AREA_RATIO):
            continue

        valid_shapes.append({
            "label": s.get("label", "Issue"),
            "pts": pts
        })

    # STEP 5 — fallback
    if not valid_shapes:
        margin = int(min(w, h) * 0.15)
        valid_shapes = [{
            "label": "Issue",
            "pts": [
                (margin, margin),
                (w - margin, margin),
                (w - margin, h - margin),
                (margin, h - margin),
                (margin, margin),
            ]
        }]

    for s in valid_shapes:
        pts = chaikin_smooth(s["pts"], 3)

        # STEP 2 — fill then stroke with SAME points
        fd.polygon(pts, fill=(255, 215, 0, 70))
        sd.line(pts, fill=(255, 215, 0, 255), width=10, joint="curve")

        # STEP 3 — label placement
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        min_x, min_y = min(xs), min(ys)

        label_x = clamp(min_x, 0, w - 1)
        label_y = clamp(min_y - font_size - 10, 0, h - 1)

        bbox = ld.textbbox((0, 0), s["label"], font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        pad = 8
        ld.rectangle(
            [label_x - pad, label_y - pad,
             label_x + tw + pad, label_y + th + pad],
            fill=(0, 0, 0, 180)
        )
        ld.text((label_x, label_y), s["label"], fill="white", font=font)

    img.alpha_composite(fill_overlay)
    img.alpha_composite(stroke_overlay)
    img.alpha_composite(label_overlay)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")
