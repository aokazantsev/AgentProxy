import os

from PIL import Image, ImageDraw

S = 1024
BACKGROUND = (15, 23, 42, 255)
LINE = (148, 163, 184, 255)
ACCENT = (56, 189, 248, 255)
NODE = (241, 245, 249, 255)

image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((32, 32, S - 32, S - 32), radius=220, fill=BACKGROUND)

y = S // 2
draw.line((210, y, S - 210, y), fill=LINE, width=64)
for x in (210, S - 210):
    draw.ellipse((x - 82, y - 82, x + 82, y + 82), fill=LINE)
half = 190
draw.polygon([(S // 2, y - half), (S // 2 + half, y), (S // 2, y + half), (S // 2 - half, y)], fill=ACCENT)
draw.ellipse((S // 2 - 70, y - 70, S // 2 + 70, y + 70), fill=NODE)

image.save(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "app.ico"),
           sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
