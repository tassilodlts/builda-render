import io
import json
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()

def percent(value, total):
    return int((value / 100) * total)

@app.post("/render")
async def render(image: UploadFile = File(...), spec: str = Form(...)):
    img_bytes = await image.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

    data = json.loads(spec)
    draw = ImageDraw.Draw(img)

    width, height = img.size

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 16)
    except:
        font = ImageFont.load_default()

    for item in data["annotations"]:
        x = percent(item["x"], width)
        y = percent(item["y"], height)
        w = percent(item["w"], width)
        h = percent(item["h"], height)

        draw.rectangle(
            [x, y, x + w, y + h],
            outline="white",
            width=3
        )

        draw.text((x, y - 18), item["text"], fill="white", font=font)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")
