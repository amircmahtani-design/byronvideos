"""Generate clearly labelled geometric QA fixtures, not the user's artwork."""
from pathlib import Path
import argparse
from PIL import Image, ImageDraw, ImageFont

SCRIPT = """This is a synthetic demonstration, not a finished essay. Four simple illustrations test the sequence.

The complete image remains visible, while a gentle movement brings the composition to life.

The narration follows these exact words. Captions appear beneath the artwork, with room to breathe.

A soft dissolve connects each image. This short test verifies the machinery, not the final artistic style."""


def make_demo(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "script.txt").write_text(SCRIPT, encoding="utf-8")
    font = ImageFont.truetype(str(Path(__file__).resolve().parent.parent / "style-guide/fonts/DejaVuSans.ttf"), 28)
    for index, size in enumerate(((1000, 700), (700, 1000), (900, 900), (1200, 600))):
        image = Image.new("RGB", size, [(91, 14, 20), (37, 57, 68), (53, 65, 45), (74, 48, 62)][index])
        draw = ImageDraw.Draw(image)
        draw.rectangle((4, 4, size[0] - 5, size[1] - 5), outline=(200, 169, 106), width=10)
        draw.ellipse((size[0]*.22, size[1]*.22, size[0]*.78, size[1]*.78), fill=(200,169,106))
        draw.polygon([(size[0]*.1,size[1]*.75),(size[0]*.5,size[1]*.3),(size[0]*.9,size[1]*.75)], fill=(239,230,210))
        draw.text((size[0]/2, 55), f"SYNTHETIC TEST IMAGE {index+1}", font=font, anchor="mm", fill=(239,230,210))
        draw.text((size[0]/2, size[1]-55), "NOT THE USER'S PAINTING", font=font, anchor="mm", fill=(239,230,210))
        image.save(destination / f"painting-{index+1}.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    make_demo(parser.parse_args().destination)
