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

# Low champion bars: real ones (Kayle at 12% and 25%) stay; red damage numbers are not an 8% champion.
got = units("kayle_12pct")
print("kayle 12%:", got)
assert any(k == "champion" and t == "enemy" and hp < 0.16 for k, t, hp in got), got
got = units("kayle_25pct")
print("kayle 25%:", got)
assert any(k == "champion" and t == "enemy" and 0.2 < hp < 0.3 for k, t, hp in got), got
got = units("damage_numbers")
print("damage numbers:", got)
assert not any(k == "champion" and t == "enemy" and hp < 0.3 for k, t, hp in got), got
print("VISION OK (low bars)")

# Ability readiness: Q with 0.6 s left (white countdown text on a dark icon) is not ready.
from jev.vision import VisionReader as _VR
_v = _VR()
hud = _v.read_hud(cv2.cvtColor(cv2.imread("tests/fixtures/frames/q_cooldown_06.jpg"), cv2.COLOR_BGR2BGRA))
print("q cooldown:", hud.ready, {k: round(v, 2) for k, v in hud.lit.items()})
assert not hud.ready.get("Q") and hud.ready.get("W") and hud.ready.get("E"), hud.ready
hud = _v.read_hud(cv2.cvtColor(cv2.imread("tests/fixtures/frames/qwe_ready.jpg"), cv2.COLOR_BGR2BGRA))
print("qwe ready:", hud.ready)
assert hud.ready.get("Q") and hud.ready.get("W") and hud.ready.get("E") and not hud.ready.get("R"), hud.ready
print("VISION OK (hud)")

# Yasuo's Q icon: the whirlwind (two stacks) against the blade.
hud = _v.read_hud(cv2.cvtColor(cv2.imread("tests/fixtures/frames/q3_tornado.jpg"), cv2.COLOR_BGR2BGRA))
print("q3 icon:", hud.q3)
assert hud.q3 is True
hud = _v.read_hud(cv2.cvtColor(cv2.imread("tests/fixtures/frames/qwe_ready.jpg"), cv2.COLOR_BGR2BGRA))
assert hud.q3 is False, hud.q3
print("VISION OK (q3)")
