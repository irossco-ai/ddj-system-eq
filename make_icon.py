"""Render icon.ico (green circle with a fader cap - same glyph as the tray icon)."""
from PIL import Image, ImageDraw

sizes = [16, 32, 48, 64, 128, 256]
frames = []
for s in sizes:
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, s // 10)
    d.ellipse((pad, pad, s - pad, s - pad), fill=(46, 204, 113, 255))
    w, h = s * 0.19, s * 0.38
    d.rectangle(((s - w) / 2, (s - h) / 2, (s + w) / 2, (s + h) / 2), fill=(255, 255, 255, 220))
    frames.append(img)
frames[-1].save("icon.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
print("icon.ico written")
