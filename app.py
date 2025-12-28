import io
import json
from typing import Any, Dict, List, Union

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()


def percent_to_px(value: Union[int, float], total: int) -> int:
    # value is expected 0..100 (percent of total)
    return int((float(value) / 100.0) * total)


def safe_load_spec(spec: str) -> Dict[str, Any]:
    """
    Accepts:
    1) JSON string:
       {"annotations":[{"x":10,"y":10,"w":30,"h":20,"text":"Crack"}]}
       or {"problem":"Crack in wall"}  (no annotations)
       or {"problem":"Crack in wall","annotations":[...]}
    2) Plain text:
       "Crack in interior wall, old house in Spain"

    Returns a dict.
    """
    spec = (spec or "").strip()

    # Try JSON first
    try:
        data = json.loads(spec) if spec else {}
        if isinstance(data, dict):
            return data
        # If JSON is not an object, wrap it
        return {"problem": str(data)}
    except Exception:
        # Plain text fallback
        return {"problem": spec}


def normalize_annotations(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Ensures data["annotations"] is a list of dicts.
    If missing, returns [].
    """
    anns = data.get("annotations", [])
    if not isinstance(anns, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in anns:
        if isinstance(item, dict):
            cleaned.append(item)
    return cleaned


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def to_pixels(value: Any, total: int) -> int:
    """
    Supports either:
    - percent values 0..100 (recommended)
    - pixel values if you pass {"unit":"px"} in the annotation (optional)
    """
    try:
        return int(value)
    except Exception:
        return 0


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

    data = safe_load_spec(spec)
    annotations = normalize_annotations(data)

    draw = ImageDraw.Draw(img)
    width, height = img.size

    # Load font (works even if DejaVuSans.ttf is not available)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    # If there are NO annotations but we have a problem text,
    # we still return a useful image (label at top-left).
    problem_text = str(data.get("problem", "")).strip()
    if not annotations and problem_text:
        box_w = min(600, width - 20)
        draw.rectangle([10, 10, 10 + box_w, 10 + 35], outline="white", width=2)
        draw.text((20, 18), problem_text[:80], fill="white", font=font)

    # Draw annotations if provided
    for item in annotations:
        # Defaults if missing
        x_val = item.get("x", 5)
        y_val = item.get("y", 5)
        w_val = item.get("w", 30)
        h_val = item.get("h", 20)
        label = str(item.get("text", "Issue"))

        # If you ever want pixel mode later, you can pass: {"unit":"px"}
        unit = str(item.get("unit", "pct")).lower()

        if unit == "px":
            x = to_pixels(x_val, width)
            y = to_pixels(y_val, height)
            w = to_pixels(w_val, width)
            h = to_pixels(h_val, height)
        else:
            # Default: percent mode (0..100)
            x = percent_to_px(x_val, width)
            y = percent_to_px(y_val, height)
            w = percent_to_px(w_val, width)
            h = percent_to_px(h_val, height)

        # Convert to corners + clamp
        x1 = clamp(x, 0, width - 1)
        y1 = clamp(y, 0, height - 1)
        x2 = clamp(x + max(w, 1), 0, width - 1)
        y2 = clamp(y + max(h, 1), 0, height - 1)

        # Ensure x2>x1 and y2>y1
        if x2 <= x1:
            x2 = clamp(x1 + 1, 0, width - 1)
        if y2 <= y1:
            y2 = clamp(y1 + 1, 0, height - 1)

        draw.rectangle([x1, y1, x2, y2], outline="white", width=3)
        draw.text((x1, max(0, y1 - 18)), label, fill="white", font=font)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")

