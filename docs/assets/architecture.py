"""Draws docs/assets/architecture.gif (English) and architecture.zh-CN.gif: ouroloop as the loop it is, a track that
sessions and candidates run around. Online parts sit on the top, offline parts on the bottom; inside the loop, the
decision models each point is routed to and the lineage that grows with every promotion. Needs Pillow and the
Helvetica Neue and Hiragino Sans GB fonts of macOS:

    python docs/assets/architecture.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2                                   # drawn at twice the size, then scaled down
W, H = 1280, 880
FRAME_MS = 70
FONTS = {"en": ("/System/Library/Fonts/HelveticaNeue.ttc", 0, 1),
         "zh": ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, 1)}
LANG = ["en"]

PAPER, INK, MUTED, RULE = "#FBFAF7", "#1E2330", "#6B6A64", "#D8D0C2"
BAND, DASH, CHEVRON = "#EDE7DB", "#CDBFA8", "#B3A68F"
EMBER, GOOD, BAD = "#E0662F", "#2E7D32", "#C62828"
ONLINE = ("#DDF0EA", "#2F8F74", "#134E3F", "#2F6F5E")      # fill, stroke, title, text
OFFLINE = ("#E9E6F7", "#6A5BC6", "#2E2573", "#4B3FA0")

X0, Y0, X1, Y1, R = 110, 110, 1170, 590, 110              # the track's centre line, a rounded rectangle

# name: (palette, centre x, centre y, width, height)
STATIONS = {
    "agent": (ONLINE, 296, Y0, 156, 92), "runtime": (ONLINE, 468, Y0, 156, 92), "env": (ONLINE, 640, Y0, 156, 92),
    "ledger": (ONLINE, 812, Y0, 156, 92), "monitor": (ONLINE, 984, Y0, 156, 92),
    "schedule": (OFFLINE, X1, 350, 180, 96),
    "research": (OFFLINE, 984, Y1, 156, 92), "data": (OFFLINE, 812, Y1, 156, 92), "train": (OFFLINE, 640, Y1, 156, 92),
    "calib": (OFFLINE, 468, Y1, 156, 92), "gate": (OFFLINE, 296, Y1, 156, 92),
    "release": (OFFLINE, X0, 350, 180, 96),
}
PANEL = (380, 196, 760, 318)            # the decision models, under the runtime
LINEAGE = (230, 404, 760, 492)
EXPERIMENTS = (780, 344, 1060, 496)     # the research agent's tries, above it
BASE = [("scenario", 20, 664, 420, 744), ("plugins", 440, 664, 840, 744), ("limits", 860, 664, 1260, 744),
        ("refs", 20, 762, 420, 842), ("versions", 440, 762, 840, 842), ("robust", 860, 762, 1260, 842)]

TEXT = {
    "en": {
        "agent": ("Business agent", ["harness and decision", "points: ask, outcome"]),
        "runtime": ("Runtime", ["default goes first;", "model acts if sure"]),
        "env": ("Environment", ["the right answers,", "probes, checkers"]),
        "ledger": ("Ledger", ["every decision,", "route and outcome"]),
        "monitor": ("Monitor", ["harmed sessions:", "revoke, roll back"]),
        "schedule": ("Schedule", ["train when default", "errors double; stop", "at target or budget"]),
        "research": ("Research agent", ["diagnoses, runs", "experiments on dev"]),
        "data": ("Data", ["built from ledger;", "test rows stay home"]),
        "train": ("Training jobs", ["local, ssh, RunPod,", "Vast or distributed"]),
        "calib": ("Calibration", ["temperature and", "per-session risk"]),
        "gate": ("Frozen gate", ["judged once on fresh", "sessions: gain, risk"]),
        "release": ("Registry & release", ["lineage, champions;", "model · spec · harness"]),
        "panel": ("Decision models", "routed per decision point, loaded by ref"),
        "point": "point", "lineage": "Lineage", "champion": "champion", "candidate": "candidate",
        "online": "online · every session", "offline": "offline · every round",
        "scenario": ("Scenario packs & task suites", ["decision points, default answerers,",
                                                     "environments, verifiers, held-out tasks"]),
        "plugins": ("Plugins", ["trainers, compute, transports, model", "backends, answerers, research LLMs"]),
        "limits": ("Human-set limits", ["frozen gate template, risk budget,", "target and training budget"]),
        "refs": ("Model refs", ["job id, or repo@sha: one key for", "calibrations, pins and revocations"]),
        "versions": ("Versions", ["git HEADs of the code that trained,", "gate record hashes, release triples"]),
        "robust": ("Robustness", ["deadlines, disconnects, lost jobs,", "cancelled orphans, retries"]),
        "ok": "within budget", "doubled": "errors x2", "tries": "3 tries", "rejected": "rejected",
        "accepted": "accepted",
        "experiments": ("Experiments on dev data", "only the best one goes to the gate"),
        "tries_1": ["recipe 1", "recipe 2", "view 3"], "tries_2": ["recipe 4", "view 5", "recipe 6"],
    },
    "zh": {
        "agent": ("业务 agent", ["harness 与决策点", "ask / outcome"]),
        "runtime": ("运行时", ["默认先答", "有把握且不同才接管"]),
        "env": ("环境", ["一组对的答案", "探针、检查器"]),
        "ledger": ("账本", ["决策、路由、结果", "只追加"]),
        "monitor": ("监控", ["按局计被害", "撤销并回退"]),
        "schedule": ("日程", ["默认错误翻倍才训练", "到目标或预算即停"]),
        "research": ("研究 agent", ["诊断，设计实验", "开发集上多次尝试"]),
        "data": ("数据", ["从账本构建", "测试与校准不外发"]),
        "train": ("训练任务", ["本机、ssh 主机", "RunPod、Vast"]),
        "calib": ("校准", ["温度", "按局风险阈值"]),
        "gate": ("冻结的门", ["新 session 判一次", "增益、风险、回归"]),
        "release": ("注册表与发布", ["谱系、各决策点冠军", "模型、spec、harness"]),
        "panel": ("决策模型", "按决策点路由，按模型引用加载"),
        "point": "决策点", "lineage": "谱系", "champion": "冠军", "candidate": "候选",
        "online": "在线：每个 session", "offline": "离线：每一轮",
        "scenario": ("场景包与任务集", ["决策点、默认回答者", "环境、验证器、held-out 任务"]),
        "plugins": ("可插拔组件", ["训练器、算力、传输方式、模型后端、", "默认回答者、研究 agent 的 LLM"]),
        "limits": ("人定的约束", ["冻结的门模板、风险预算", "目标与训练预算"]),
        "refs": ("模型引用", ["job id 或 repo@sha：校准、", "session 锁定和撤销都用它"]),
        "versions": ("版本", ["训练代码的 git HEAD、", "门记录哈希、发布三元组"]),
        "robust": ("鲁棒性", ["超时、断线、丢失检测、", "取消孤儿任务、重试"]),
        "ok": "在预算内", "doubled": "默认错误 ×2", "tries": "3 次实验", "rejected": "拒绝", "accepted": "通过",
        "experiments": ("开发集上的实验", "只把最好的一个送去过门"),
        "tries_1": ["配方 1", "配方 2", "视图 3"], "tries_2": ["配方 4", "视图 5", "配方 6"],
    },
}

_FONTS: dict = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path, regular, heavy = FONTS[LANG[0]]
    key = (path, size, bold)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(path, size * S, index=heavy if bold else regular)
    return _FONTS[key]


def sc(v):
    return [c * S for c in v]


def text(d, xy, s, size=14, color=MUTED, bold=False, anchor="la") -> None:
    d.text(sc(xy), s, font=font(size, bold), fill=color, anchor=anchor)


def width(d, s, size=14, bold=False) -> float:
    return d.textlength(s, font=font(size, bold)) / S


# ---------------------------------------------------------------------- the track
def centre_line(step: float = 2.0) -> list[tuple[float, float]]:
    """The track's centre line, clockwise from the start of the top straight."""
    pts = []

    def straight(a, b):
        n = max(1, int(math.dist(a, b) / step))
        pts.extend((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n))

    def corner(cx, cy, a0):
        n = int(math.pi / 2 * R / step)
        pts.extend((cx + R * math.cos(a0 + math.pi / 2 * i / n), cy + R * math.sin(a0 + math.pi / 2 * i / n))
                   for i in range(n))

    straight((X0 + R, Y0), (X1 - R, Y0))
    corner(X1 - R, Y0 + R, -math.pi / 2)
    straight((X1, Y0 + R), (X1, Y1 - R))
    corner(X1 - R, Y1 - R, 0)
    straight((X1 - R, Y1), (X0 + R, Y1))
    corner(X0 + R, Y1 - R, math.pi / 2)
    straight((X0, Y1 - R), (X0, Y0 + R))
    corner(X0 + R, Y0 + R, math.pi)
    return pts


LINE = centre_line()


def at(i: float) -> tuple[float, float]:
    """The point i samples along the centre line (it wraps around)."""
    n = len(LINE)
    i %= n
    a, b = LINE[int(i)], LINE[(int(i) + 1) % n]
    k = i - int(i)
    return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k


def index_of(name: str) -> int:
    _, cx, cy, _, _ = STATIONS[name]
    return min(range(len(LINE)), key=lambda i: math.dist(LINE[i], (cx, cy)))


def station(d, name, lang, active=False) -> None:
    (fill, stroke, tcol, scol), cx, cy, w, h = STATIONS[name]
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d.rounded_rectangle(sc((x0, y0, x1, y1)), radius=12 * S, fill=fill, outline=EMBER if active else stroke,
                        width=round((3 if active else 1.4) * S))
    title, lines = TEXT[lang][name]
    text(d, (x0 + 12, y0 + 10), title, 17, tcol, True)
    for i, line in enumerate(lines):
        y = y0 + 36 + i * 18
        text(d, (x0 + 12, y), line, 14, scol)
        if width(d, line) > w - 20 or y + 17 > y1 - 3:
            raise ValueError(f"{lang} {name}: {line!r} does not fit")
    if width(d, title, 17, True) > w - 20:
        raise ValueError(f"{lang} {name}: title {title!r} does not fit")


def chevrons(d) -> None:
    for k in range(0, len(LINE), 46):
        (x, y), (x2, y2) = at(k), at(k + 4)
        a = math.atan2(y2 - y, x2 - x)
        for side in (-1, 1):
            b = a + math.pi - side * 0.7
            d.line(sc((x + 6 * math.cos(a), y + 6 * math.sin(a), x + 6 * math.cos(a) + 7 * math.cos(b),
                       y + 6 * math.sin(a) + 7 * math.sin(b))), fill=CHEVRON, width=round(1.6 * S))


def static(lang: str) -> Image.Image:
    LANG[0] = lang
    t = TEXT[lang]
    img = Image.new("RGB", (W * S, H * S), PAPER)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(sc((X0 - 22, Y0 - 22, X1 + 22, Y1 + 22)), radius=(R + 22) * S, fill=BAND)
    d.rounded_rectangle(sc((X0 + 22, Y0 + 22, X1 - 22, Y1 - 22)), radius=(R - 22) * S, fill=PAPER)
    for k in range(0, len(LINE), 8):
        a, b = at(k), at(k + 4)
        d.line(sc(a + b), fill=DASH, width=S)
    chevrons(d)
    text(d, (160, 168), t["online"], 14, MUTED)
    text(d, (962, 528), t["offline"], 14, MUTED, anchor="rs")
    for name in STATIONS:
        station(d, name, lang)
    x0, y0, x1, y1 = PANEL                                  # the decision models, which the runtime asks
    d.rounded_rectangle(sc(PANEL), radius=12 * S, fill="#FFFFFF", outline=RULE, width=round(1.2 * S))
    title, sub = t["panel"]
    text(d, (x0 + 14, y0 + 12), title, 16, INK, True)
    text(d, (x0 + 14, y0 + 36), sub, 14, MUTED)
    for dx in (-8, 8):
        d.line(sc((468 + dx, Y0 + 46, 468 + dx, y0 - 2)), fill=CHEVRON, width=round(1.6 * S))
    ex0, ey0, ex1, ey1 = EXPERIMENTS                        # the research agent's tries
    d.rounded_rectangle(sc(EXPERIMENTS), radius=12 * S, fill="#FFFFFF", outline=RULE, width=round(1.2 * S))
    title, sub = t["experiments"]
    text(d, (ex0 + 14, ey0 + 12), title, 16, INK, True)
    text(d, (ex0 + 14, ey0 + 36), sub, 14, MUTED)
    for dx in (-8, 8):
        d.line(sc((984 + dx, ey1 + 2, 984 + dx, Y1 - 46)), fill=CHEVRON, width=round(1.6 * S))
    lx0, ly0, lx1, ly1 = LINEAGE                            # the lineage, which the registry keeps
    text(d, (lx0, ly0 - 26), t["lineage"], 15, INK, True)
    d.line(sc((X0 + 90, 350, lx0 + 8, 350)), fill=CHEVRON, width=round(1.4 * S))
    d.line(sc((lx0 + 8, 350, lx0 + 8, ly0 + 22)), fill=CHEVRON, width=round(1.4 * S))
    for name, bx0, by0, bx1, by1 in BASE:
        d.rounded_rectangle(sc((bx0, by0, bx1, by1)), radius=10 * S, fill="#FFFFFF", outline=RULE,
                            width=round(1.2 * S))
        title, lines = t[name]
        text(d, (bx0 + 14, by0 + 10), title, 15, INK, True)
        for i, line in enumerate(lines):
            text(d, (bx0 + 14, by0 + 34 + i * 19), line, 14, MUTED)
            if width(d, line) > bx1 - bx0 - 24:
                raise ValueError(f"{lang} {name}: {line!r} does not fit")
    return img


# ---------------------------------------------------------------------- one round, in seconds
def dot(d, xy, color=EMBER, r=8) -> None:
    x, y = xy
    d.ellipse(sc((x - r, y - r, x + r, y + r)), fill=color, outline=PAPER, width=round(2 * S))


def badge(d, name, label, color) -> None:
    _, cx, cy, w, h = STATIONS[name]
    bw = width(d, label, 13, True) + 18
    x1, y0 = cx + w / 2 - 6, cy - h / 2 - 12
    d.rounded_rectangle(sc((x1 - bw, y0, x1, y0 + 24)), radius=12 * S, fill=color)
    text(d, (x1 - bw / 2, y0 + 12), label, 13, "#FFFFFF", True, "mm")


def chip(d, x, y, label, color, fill="#FFFFFF", crossed=False) -> float:
    w = width(d, label, 14, True) + 26
    d.rounded_rectangle(sc((x, y, x + w, y + 30)), radius=15 * S, fill=fill, outline=color, width=round(1.6 * S))
    text(d, (x + w / 2, y + 15), label, 14, color, True, "mm")
    if crossed:
        tw = width(d, label, 14, True)
        d.line(sc((x + w / 2 - tw / 2 - 3, y + 16, x + w / 2 + tw / 2 + 3, y + 16)), fill=color, width=round(1.6 * S))
    return w


def lineage(d, t, stage) -> None:
    """stage 0: v1; 1: and a rejected candidate after it; 2: and v2, accepted."""
    lx0, ly0, _, _ = LINEAGE
    y, x = ly0 + 8, lx0 + 24
    x += chip(d, x, y, f"v1 · {t['champion']}" if stage < 2 else "v1", OFFLINE[1] if stage < 2 else MUTED,
              OFFLINE[0] if stage < 2 else "#FFFFFF")
    if stage >= 1:
        d.line(sc((x, y + 15, x + 34, y + 15)), fill=CHEVRON, width=round(1.4 * S))
        x += 34 + chip(d, x + 34, y, t["candidate"], BAD, "#FBEAEA", crossed=True)
    if stage >= 2:
        d.line(sc((x, y + 15, x + 34, y + 15)), fill=CHEVRON, width=round(1.4 * S))
        chip(d, x + 34, y, f"v2 · {t['champion']}", OFFLINE[1], OFFLINE[0])


def models(d, t, v2: bool, flash: bool) -> None:
    x0, y0, _, _ = PANEL
    x = x0 + 14
    for k, (pt, ver) in enumerate(((f"{t['point']} A", "v2" if v2 else "v1"), (f"{t['point']} B", "v1"))):
        hot = flash and k == 0
        x += chip(d, x, y0 + 70, f"{pt} → {ver}" if LANG[0] == "zh" else f"{pt}: {ver}", EMBER if hot else ONLINE[1], "#FDEEE6" if hot else ONLINE[0]) + 12


def experiments(d, t, key, shown: int, chosen: int | None) -> None:
    ex0, ey0, _, _ = EXPERIMENTS
    x, y = ex0 + 14, ey0 + 66
    for k, label in enumerate(t[key][:shown]):
        hot = k == chosen
        w = chip(d, x, y, label, EMBER if hot else OFFLINE[1], "#FDEEE6" if hot else OFFLINE[0])
        x += w + 10
        if k == 1:
            x, y = ex0 + 14, y + 40


def frame(base: Image.Image, lang: str, t: float, idx: dict) -> Image.Image:
    LANG[0] = lang
    tx = TEXT[lang]
    img = base.copy()
    d = ImageDraw.Draw(img)
    n = len(LINE)
    lit, dots, badges = set(), [], []
    lineage_stage, v2, flash, progress, tries = 0, False, False, None, None

    def move(a, b, t0, t1):
        """A dot from station a to station b, clockwise; the stations it passes light up."""
        if t0 <= t <= t1:
            here = idx[a] + ((idx[b] - idx[a]) % n) * (t - t0) / (t1 - t0)
            dots.append(at(here))
            for name, i in idx.items():
                if abs(((here - i + n / 2) % n) - n / 2) < 40:
                    lit.add(name)

    def sessions(t0, count):
        """Sessions: agent -> runtime (which asks the decision models) -> environment -> ledger -> monitor."""
        for k in range(count):
            move("agent", "monitor", t0 + k * 0.6, t0 + 2.4 + k * 0.6)
            s0 = t0 + 0.5 + k * 0.6
            if s0 <= t <= s0 + 0.6:
                f = (t - s0) / 0.6
                dots.append((468, Y0 + 46 + (PANEL[1] - Y0 - 46) * (1 - abs(2 * f - 1))))

    def round_(t0, key, chosen, verdict):
        """The schedule fires, the research agent tries three changes on dev data and sends the best to training,
        and the one candidate goes through the gate record of the round."""
        nonlocal progress, tries
        move("monitor", "schedule", t0, t0 + 0.7)
        if t0 + 0.7 <= t < t0 + 1.4:
            lit.add("schedule")
            badges.append(("schedule", tx["doubled"], EMBER))
        move("schedule", "research", t0 + 1.3, t0 + 1.8)
        if t0 + 1.8 <= t < t0 + 5.0:
            tries = (key, min(3, 1 + int((t - t0 - 1.9) / 0.3)) if t >= t0 + 1.9 else 0,
                     chosen if t >= t0 + 2.9 else None)
        if t0 + 1.8 <= t < t0 + 3.2:
            lit.add("research")
            badges.append(("research", tx["tries"], EMBER))
        move("research", "train", t0 + 3.2, t0 + 3.9)
        if t0 + 3.9 <= t < t0 + 5.0:
            lit.add("train")
            progress = min(1.0, (t - t0 - 3.9) / 1.0)
        move("train", "gate", t0 + 5.0, t0 + 5.7)
        if t0 + 5.7 <= t < t0 + 6.6:
            lit.add("gate")
            badges.append(("gate", tx[verdict], GOOD if verdict == "accepted" else BAD))

    sessions(0.4, 3)
    if 3.6 <= t < 4.4:
        lit.add("monitor")
        badges.append(("monitor", tx["ok"], GOOD))
    round_(4.2, "tries_1", 1, "rejected")
    if t >= 10.0:
        lineage_stage = 1
    sessions(10.6, 2)
    round_(12.6, "tries_2", 2, "accepted")
    move("gate", "release", 19.0, 19.7)                     # the registry records v2 ...
    if t >= 19.7:
        lineage_stage = 2
    if 19.7 <= t < 20.4:
        lit.add("release")
    move("release", "runtime", 19.9, 20.8)                  # ... and the sessions opened from then on run it
    if t >= 20.8:
        v2, flash = True, t < 21.8
    sessions(21.0, 3)

    for name in lit:
        station(d, name, lang, active=True)
    if progress is not None:
        _, cx, cy, w, h = STATIONS["train"]
        x0, y0, y1 = cx - w / 2 + 12, cy + h / 2 - 12, cy + h / 2 - 6
        d.rounded_rectangle(sc((x0, y0, cx + w / 2 - 12, y1)), radius=3 * S, fill="#D9D3F2")
        if progress > 0:
            d.rounded_rectangle(sc((x0, y0, x0 + (w - 24) * progress, y1)), radius=3 * S, fill=EMBER)
    for name, label, color in badges:
        badge(d, name, label, color)
    if tries:
        experiments(d, tx, *tries)
    lineage(d, tx, lineage_stage)
    models(d, tx, v2, flash)
    for xy in dots:
        dot(d, xy)
    return img.resize((W, H), Image.LANCZOS)


END = 25.0
STILL = 17.4                            # the moment the PNG shows


def render(lang: str, out: Path) -> None:
    base = static(lang)
    idx = {name: index_of(name) for name in STATIONS}
    frames = [frame(base, lang, i * FRAME_MS / 1000, idx) for i in range(int(END * 1000 / FRAME_MS))]
    still = frames[int(STILL * 1000 / FRAME_MS)]
    sample = Image.new("RGB", (W, H + 10))
    sample.paste(still, (0, 0))
    strip = ImageDraw.Draw(sample)
    for k, c in enumerate((EMBER, GOOD, BAD, "#FDEEE6", "#D9D3F2", OFFLINE[0], ONLINE[0])):
        strip.rectangle((k * 20, H, k * 20 + 19, H + 9), fill=c)
    pal = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    gif[0].save(out, save_all=True, append_images=gif[1:], duration=FRAME_MS, loop=0, optimize=True)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    render("en", here / "architecture.gif")
    render("zh", here / "architecture.zh-CN.gif")
