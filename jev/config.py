"""Tunables. Screen geometry is in physical pixels of the captured frame; screen.py converts."""
from __future__ import annotations

from dataclasses import dataclass

# Summoner's Rift, map units. Approximate; the pathfinder does the real work.
MAP_W, MAP_H = 14820.0, 14881.0
BLUE_FOUNTAIN = (400.0, 400.0)
RED_FOUNTAIN = (14300.0, 14400.0)
MID_CENTER = (7400.0, 7400.0)
BLUE_MID_T1 = (5846.0, 6396.0)
RED_MID_T1 = (8955.0, 8510.0)

# Structures (map units, approximate). Used to mask their minimap icons and for tower safety.
BLUE_TOWERS = [(981, 10441), (1512, 6699), (1169, 4287), (5846, 6396), (5048, 4812), (3651, 3696),
               (10504, 1029), (6919, 1483), (4281, 1253), (2177, 1807), (1748, 2270)]
RED_TOWERS = [(4318, 13875), (7943, 13411), (10481, 13650), (8955, 8510), (9767, 10113), (11134, 11207),
              (13866, 4505), (13327, 8226), (13624, 10572), (12611, 13084), (13052, 12612)]
BLUE_STRUCTURES = BLUE_TOWERS + [(1171, 3571), (3203, 3208), (3452, 1236), (1551, 1659)]
RED_STRUCTURES = RED_TOWERS + [(11261, 13676), (11598, 11667), (13604, 11316), (13270, 13230)]
TOWER_RANGE = 775.0 + 150.0   # tower range plus a margin

# Progress along the mid-lane diagonal, 0 = own fountain, 1 = enemy fountain.
LANE_CENTER = 0.50
OWN_TOWER = 0.42
ENEMY_TOWER = 0.60
MAX_ADVANCE = 0.48          # default farming limit; Jev's aggression moves it between 0.45 and 0.52
HARD_LIMIT = 0.53           # enemy tower range edge is ~0.56; never farm past this
PUSH_ADVANCE = 0.54         # push_tower may go this far (minions tank the tower)
DIAGONAL_UNITS = 19660.0    # fountain to fountain


@dataclass
class Geometry:
    """Where things are on screen. Calibrate with `jev snapshot` once the game runs."""

    # Where the locked camera puts the champion, in frame pixels. None = frame centre.
    champion_px: tuple[int, int] | None = (862, 490)
    # Optional minimap square (x, y, side) in frame pixels. None = use screen-relative clicks only.
    minimap: tuple[int, int, int] | None = (1350, 705, 372)
    # Optional shop search box (x, y) in frame pixels. None = shopping disabled.
    shop_search: tuple[int, int] | None = (676, 242)
    starter_card: tuple[int, int] | None = (676, 400)      # "Doran's Blade Start" recommended card
    purchase_button: tuple[int, int] | None = (1167, 582)
    shop_close: tuple[int, int] | None = (1340, 183)
    shop_undo: tuple[int, int] | None = (530, 836)       # UNDO under the item grid (reverts the last buy)
    search_result: tuple[int, int] | None = (490, 320)     # first tile in the search RESULTS list
    shop_button: tuple[int, int] | None = (1181, 1063)     # HUD "P" shop icon
    recall_button: tuple[int, int] | None = (1181, 1020)   # HUD "B" recall icon
    boots_card: tuple[int, int] | None = (418, 765)        # first "commonly built" icon (boots)
    # HUD ability icons and the level-up chevrons above them, Q W E R
    ability_icons: tuple[tuple[int, int], ...] = ((673, 985), (737, 985), (799, 985), (861, 985))
    level_chevrons: tuple[tuple[int, int], ...] = ((675, 935), (737, 935), (799, 935), (861, 935))
    move_click_px: int = 420        # how far ahead to click when walking
    attack_move_px: int = 260       # how far ahead to attack-move at the wave
    q_cast_px: int = 300            # blind Q distance up the lane
    retreat_click_px: int = 480


@dataclass
class Timing:
    tick_hz: float = 5.0
    brain_hz: float = 1.0
    move_reissue_s: float = 1.0
    q_period_s: float = 1.6
    recall_channel_s: float = 8.6
    respawn_shop_s: float = 2.5
    damage_window_s: float = 3.0
    heavy_damage_pct: float = 12.0  # HP% lost within the window that counts as heavy
    retreat_hold_s: float = 7.0     # keep retreating this long after a safety trigger
    no_recall_after_base_s: float = 25.0
    max_reckon_speed: float = 400.0
    seek_after_s: float = 10.0      # no minion contact this long -> creep forward
    seek_step: float = 0.0035       # per farm tick at 5 Hz: the limit moves at about walking speed
    seek_max: float = 0.06          # patrol forward this far past the limit
    seek_back: float = 0.03         # then back this far toward the own tower
    resync_s: float = 14.0          # mid-game start: walk to own tower this long first


@dataclass
class Vision:
    """Health-bar and HUD reading, measured on the 1728x1117 windowed layout (snapshots/054108.png)."""

    view: tuple[int, int, int, int] = (90, 70, 1728, 940)   # x0, y0, x1, y1 of the game view (no HUD, no sidebar)
    hud_block: tuple[int, int, int, int] = (285, 895, 1225, 1117)  # ability bar and item panel
    chat_block: tuple[int, int, int, int] = (0, 560, 500, 870)      # chat box: coloured names are not units
    target_block: tuple[int, int, int, int] = (0, 55, 355, 195)     # selected-target panel (its red HP bar read as minions)
    portrait_block: tuple[int, int, int, int] = (1335, 530, 1728, 705)  # teammate portraits above the minimap
    frame_dark_v: int = 60             # HSV value below this counts as the dark bar frame
    minion_bar_w: int = 60             # fill width of a full minion bar
    minion_bar_h: tuple[int, int] = (3, 6)
    champ_bar_w: int = 106             # fill width of a full champion bar
    champ_bar_h: tuple[int, int] = (9, 12)
    minion_body_offset: tuple[int, int] = (0, 32)    # bar centre -> where to click the minion
    champ_body_offset: tuple[int, int] = (-10, 78)   # bar centre -> champion body
    hud_icons: tuple[tuple[int, int], ...] = ((673, 985), (737, 985), (799, 985), (861, 985), (925, 985), (972, 985))
    icon_half: int = 20
    # Inventory slots 1-6 (API slot 0-5, top row then bottom row) and the trinket (API slot 6).
    item_slots: tuple[tuple[int, int], ...] = ((1044, 978), (1090, 978), (1136, 978), (1044, 1022), (1090, 1022), (1136, 1022), (1181, 980))
    item_half: int = 14
    item_ready_lit: float = 0.08       # share of bright pixels above which an item active counts as ready (calibrate)
    icon_ready_lit: float = 0.15       # share of bright non-white pixels above which an icon counts as ready
    px_per_unit: float = 0.46          # screen px per game unit at default zoom (minimap camera box)
    # Game ranges in units (current patch, approximate); edge-to-edge adds unit radius, so auto has slack.
    auto_range: float = 240.0
    q_range: float = 450.0
    q3_range: float = 1050.0
    e_range: float = 475.0
    r_range: float = 1400.0
    w_range: float = 400.0


@dataclass
class Fast:
    """The fast loop: perception, Jev tactics and input rates."""

    act_hz: float = 30.0               # actor loop rate
    min_action_gap_s: float = 0.11     # at most ~545 orders a minute
    hover_s: float = 0.012             # in-game click: cursor move -> button down (the game must see the cursor over a unit)
    aim_s: float = 0.036               # skill / summoner at a point: cursor move -> key, about two game frames
    hold_s: float = 0.012              # button / key hold
    mod_gap_s: float = 0.008           # modifier down -> key
    tactic_stale_s: float = 0.45       # drop a Jev tactical answer older than this
    eq_delay_s: float = 0.07           # E press -> Q press for the circular EQ
    r_watch_s: float = 1.0             # after a tornado, watch this long for R to light up
    windup_frac: float = 0.22          # share of an attack spent in wind-up; move only after it
    lasthit_margin: float = 1.0        # predicted HP must be below damage * margin
    lasthit_lead_s: float = 0.1        # input to the game; walk and wind-up are added per minion
    q_cast_s: float = 0.3              # Yasuo Q cast time (shortened by attack speed; approximate)
    forecast_cap: float = 40.0         # HP the forecast may count on others taking off before our hit lands
    melee_hp: tuple[float, float] = (477.0, 22.0)   # base, per 90 s (approximate)
    caster_hp: tuple[float, float] = (296.0, 8.0)
    q_base: tuple[float, ...] = (20.0, 45.0, 70.0, 95.0, 120.0)  # plus 105% AD (approximate)
    q_ad: float = 1.05


GEOMETRY = Geometry()
TIMING = Timing()
VISION = Vision()
FAST = Fast()
