"""Antialiased artwork rendered at four times the final display resolution."""

from functools import lru_cache
import math

from PIL import Image, ImageColor, ImageDraw


SUPERSAMPLE = 4


class Artwork:
    def __init__(self, width, height):
        self.width, self.height = max(1, int(width)), max(1, int(height))
        self.image = Image.new("RGBA", (self.width * SUPERSAMPLE, self.height * SUPERSAMPLE))
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def coords(values):
        return tuple(round(value * SUPERSAMPLE) for value in values)

    def line(self, points, color, width=1.6):
        scaled = [self.coords(point) for point in points]
        self.draw.line(scaled, fill=color, width=round(width * SUPERSAMPLE), joint="curve")
        radius = width / 2
        for x, y in (points[0], points[-1]):
            self.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)

    def ellipse(self, box, *, fill=None, outline=None, width=1):
        self.draw.ellipse(self.coords(box), fill=fill, outline=outline, width=round(width * SUPERSAMPLE))

    def rectangle(self, box, color, width=1.6):
        self.draw.rectangle(self.coords(box), outline=color, width=round(width * SUPERSAMPLE))

    def polygon(self, points, color):
        self.draw.polygon([self.coords(point) for point in points], fill=color)

    def arc(self, box, start, end, color, width=1.6):
        self.draw.arc(self.coords(box), start=start, end=end, fill=color, width=round(width * SUPERSAMPLE))

    def finish(self):
        return self.image.resize((self.width, self.height), Image.Resampling.LANCZOS)


@lru_cache(maxsize=256)
def render_icon(icon, size, color):
    art = Artwork(size, size)
    c = size / 2
    def line(*coords, width=1.6):
        art.line(list(zip(coords[::2], coords[1::2])), color, width)
    if icon == "edit":
        line(c-5, c+5, c+5, c-5)
        line(c-6, c+2, c-6, c+6, c-2, c+6)
        line(c+3, c-6, c+6, c-3)
    elif icon == "delete":
        art.rectangle((c-5, c-3, c+5, c+7), color)
        line(c-7, c-5, c+7, c-5)
        line(c-2, c-7, c+2, c-7)
        for x in (c-2, c+2):
            line(x, c, x, c+4)
    elif icon in ("plus", "minus"):
        line(c-6, c, c+6, c, width=2)
        if icon == "plus":
            line(c, c-6, c, c+6, width=2)
    elif icon in ("refresh", "reset"):
        art.arc((c-7, c-7, c+7, c+7), 45, 330, color, 1.8)
        art.polygon([(c+3, c-8), (c+9, c-7), (c+7, c-1)], color)
    elif icon == "import":
        art.rectangle((c-6, c+1, c+6, c+7), color)
        line(c, c-7, c, c+3)
        line(c-4, c-1, c, c+3, c+4, c-1)
    elif icon == "settings":
        points = []
        for tooth in range(8):
            for offset, radius in ((-.48, 7), (-.26, 9.5), (.26, 9.5), (.48, 7)):
                angle = (tooth + offset) * math.pi / 4
                points.append((c+radius*math.cos(angle), c+radius*math.sin(angle)))
        art.polygon(points, color)
        art.ellipse((c-4, c-4, c+4, c+4), fill=(0, 0, 0, 0))
    elif icon in ("previous", "next"):
        direction = -1 if icon == "previous" else 1
        line(c-direction*2, c-5, c+direction*3, c, c-direction*2, c+5, width=1.8)
    elif icon == "close":
        line(c-5, c-5, c+5, c+5)
        line(c+5, c-5, c-5, c+5)
    elif icon == "chart":
        line(c-7, c-6, c-7, c+7, c+7, c+7)
        line(c-4, c+3, c-1, c-1, c+2, c+1, c+6, c-5)
    elif icon == "search":
        art.ellipse((c-7, c-7, c+1, c+1), outline=color, width=1.5)
        line(c, c, c+5, c+5, width=1.5)
    elif icon == "grip":
        for offset in (3, 7, 11):
            line(size-offset, size-2, size-2, size-offset, width=1)
    else:
        art.ellipse((c-5, c-5, c+5, c+5), outline=color, width=1.6)
    return art.finish()


@lru_cache(maxsize=64)
def render_asset_ring(size, color, background):
    art = Artwork(size, size)
    art.ellipse((2, 2, size-2, size-2), fill=background, outline=color)
    return art.finish()


def render_chart_line(width, height, points, color, *, stroke=1.5, endpoint=0, fill_to=None):
    art = Artwork(width, height)
    if fill_to is not None and len(points) > 1:
        # A translucent area replaces Tk's visibly dotted stipple pattern.
        shade = ImageColor.getrgb(color) + (28,)
        art.polygon(list(points) + [(points[-1][0], fill_to), (points[0][0], fill_to)], shade)
    if len(points) > 1:
        art.line(points, color, stroke)
    if points and endpoint:
        x, y = points[-1]
        art.ellipse((x-endpoint, y-endpoint, x+endpoint, y+endpoint), fill=color)
    return art.finish()
