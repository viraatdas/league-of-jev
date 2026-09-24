"""Vision on saved frames: level-10+ enemy champions are champions (their two-digit level box
fooled the monster test and Yasuo died to "no champ", g15), a large monster's gold-framed bar is a
monster, and a monster's hover outline leaves no phantom champion.
Run: uv run python tests/offline_vision_check.py"""
import cv2

from jev.vision import VisionReader

v = VisionReader()


def units(name: str) -> list:
    f = cv2.imread(f"tests/fixtures/frames/{name}.jpg")
    us, _ = v.read_units(cv2.cvtColor(f, cv2.COLOR_BGR2BGRA))
    return [(u.kind, u.team, round(u.hp, 2)) for u in us if u.kind != "minion"]


got = units("level10_urgot_kayle")
print("urgot and kayle:", got)
champs = sorted(hp for k, t, hp in got if k == "champion" and t == "enemy")
assert len(champs) == 2 and abs(champs[0] - 0.75) < 0.04 and champs[1] > 0.9, got  # Urgot 1461/1945, Kayle full
got = units("red_buff")
print("red buff:", got)
assert got and all(k == "monster" for k, _, _ in got), got
got = units("monster_outline")
print("outline:", got)
assert not [u for u in got if u[0] == "champion"], got
print("VISION OK")

# The shop's corner is found wherever the panel was dragged.
from jev.uiscan import SHOP_CORNER_AT, shop_corner
for name, want in (("shop_home", SHOP_CORNER_AT), ("shop_dragged", (249, 349))):
    x, y, sc = shop_corner(cv2.imread(f"tests/fixtures/frames/{name}.jpg"))
    print(name, (x, y), round(sc, 2))
    assert sc >= 0.8 and abs(x - want[0]) + abs(y - want[1]) <= 4, (x, y, sc)
x, y, sc = shop_corner(cv2.imread("tests/fixtures/frames/level10_urgot_kayle.jpg"))
assert sc < 0.8, sc
print("SHOP CORNER OK")
