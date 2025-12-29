import io
import json
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def percent_to_px(value: float, total: int) -> int:
    # value is expected 0..100
    return int((float(value) / 100.0) * total)


def safe_load_spec(spec: str) -> Dict[str, Any]:
    spec = (spec or "").strip()
    try:
        data = json.loads(spec) if spec else {}
        if not isinstance(data, dict):
            raise ValueError("Spec must be a JSON object")
        return data
    except Exception:
        raise HTTPException(status_code=400, detail="Spec must be valid JSON (object)")


def load_font() -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 16)
    except Exception:
        return ImageFont.load_default()


def points_to_pixels(points: List[Dict[str, Any]], unit: str, width: int, height: int) -> List[Tuple[int, int]]:
    unit = (unit or "norm").lower()
    pts: List[Tuple[int, int]] = []

    for p in points:
        if not isinstance(p, dict):
            continue
        try:
            x_raw = float(p.get("x"))
            y_raw = float(p.get("y"))
        except Exception:
            continue

        if unit == "px":
            x = int(x_raw)
            y = int(y_raw)
        elif unit == "pct":
            x = percent_to_px(x_raw, width)
            y = percent_to_px(y_raw, height)
        else:
            # default: normalized 0..1
            x = int(clamp01(x_raw) * width)
            y = int(clamp01(y_raw) * height)

        x = clamp_int(x, 0, width - 1)
        y = clamp_int(y, 0, height - 1)
        pts.append((x, y))

    return pts


def draw_polyline(img: Image.Image, pts_px: List[Tuple[int, int]], label: str, font: ImageFont.ImageFont) -> None:
    draw = ImageDraw.Draw(img)
    width, height = img.size

    stroke = max(6, width // 220)
    color = (255, 215, 0, 255)  # yellow
    outline = (0, 0, 0, 255)

    # outline then main line for contrast
    draw.line(pts_px, fill=outline, width=stroke + 4, joint="curve")
    draw.line(pts_px, fill=color, width=stroke, joint="curve")

    r = max(6, stroke)
    for (x, y) in pts_px:
        draw.ellipse((x - r, y - r, x + r, y + r), outline=outline, width=3)
        draw.ellipse((x - r + 2, y - r + 2, x + r - 2, y + r - 2), outline=color, width=3)

    label = (label or "").strip()[:12]
    if label:
        x0, y0 = pts_px[0]
        pad_x, pad_y = 8, 6
        box_w = pad_x * 2 + max(40, len(label) * 8)
        box_h = 24

        bx1 = clamp_int(x0, 0, width - 1)
        by1 = clamp_int(y0 - box_h - 8, 0, height - 1)
        bx2 = clamp_int(bx1 + box_w, 0, width - 1)
        by2 = clamp_int(by1 + box_h, 0, height - 1)

        draw.rectangle([bx1, by1, bx2, by2], fill=(0, 0, 0, 200))
        draw.text((bx1 + pad_x, by1 + pad_y), label, fill="white", font=font)


@app.get("/")
def root():
    return {"ok": True, "service": "builda-render"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/render")
async def render(image: UploadFile = File(...), spec: str = Form(...)):
    img_bytes = await image.read()
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image upload")

    data = safe_load_spec(spec)

    # STRICT: must be polyline
    if str(data.get("type", "")).lower() != "polyline":
        raise HTTPException(status_code=400, detail='Spec.type must be "polyline"')

    points = data.get("points")
    if not isinstance(points, list):
        raise HTTPException(status_code=400, detail="Spec.points must be a list")

    unit = str(data.get("unit", "norm")).lower()
    label = str(data.get("label", "")).strip()

    font = load_font()
    width, height = img.size

    pts_px = points_to_pixels(points, unit, width, height)
    if len(pts_px) < 2:
        raise HTTPException(status_code=400, detail="Polyline needs at least 2 valid points")

    draw_polyline(img, pts_px, label, font)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")
