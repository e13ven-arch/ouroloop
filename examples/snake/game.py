"""Snake: the rules, the legal moves and the board text. Standard library only.

A state is a plain dict, so the ledger keeps it and a view can be rendered from it again offline:

    {"width": 8, "height": 8, "body": [[x, y], ...] (the head first), "food": [x, y] or None, "heading": "right"}

Rules. The snake moves one cell per step. It dies when its head leaves the board or enters its own body; the tail's
cell counts as free, since the tail moves on (food is never on the body, so eating never ends on it). Eating grows
the snake by one and puts new food on a free cell, drawn by the game's seeded random generator; with no free cell
left the board is full and the game is over. Moving back into the neck is not legal, so there are at most three
legal moves. A game starts as in the jev Snake integration: the head in the middle, heading right, the body to its
left.

The board text is the jev integration's: a header "Length n. Heading d.", then one line per row in which every cell
is one token, " ." empty, " F" food, " H" head, " 1".." 9" the body: the steps until that cell is free again (the
tail is 1; 9 means nine or more).
"""
from __future__ import annotations

import random

DIRS = ("up", "down", "left", "right")
DELTA = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
BACK = {"up": "down", "down": "up", "left": "right", "right": "left"}


def first(moves, heading: str) -> str:
    """The tie-break everywhere in the pack: keep the heading if it is among `moves`, else the first in DIRS order."""
    return heading if heading in moves else next(d for d in DIRS if d in moves)


def unpack(state: dict) -> tuple[int, int, list[tuple], tuple | None, str]:
    food = state.get("food")
    return (state["width"], state["height"], [tuple(c) for c in state["body"]], tuple(food) if food else None,
            state["heading"])


def moved(w: int, h: int, body: list[tuple], food: tuple | None, d: str) -> tuple[list[tuple] | None, bool]:
    """(the body after moving `d`, head first, or None when the move kills; whether it eats)."""
    (x, y), (dx, dy) = body[0], DELTA[d]
    head = (x + dx, y + dy)
    if not (0 <= head[0] < w and 0 <= head[1] < h):
        return None, False
    eats = head == food
    rest = body if eats else body[:-1]
    if head in rest:
        return None, eats
    return [head, *rest], eats


def legal(state: dict) -> list[str]:
    return [d for d in DIRS if d != BACK[state["heading"]]]


def render(state: dict) -> str:
    w, h, body, food, heading = unpack(state)
    age = {c: len(body) - i for i, c in enumerate(body)}
    rows = []
    for y in range(h):
        rows.append("".join(" H" if (x, y) == body[0] else f" {min(9, age[(x, y)])}" if (x, y) in age
                            else " F" if (x, y) == food else " ." for x in range(w)))
    return f"Length {len(body)}. Heading {heading}.\n" + "\n".join(rows)


class Game:
    def __init__(self, width: int = 8, height: int = 8, length: int = 3, seed: int = 0):
        self.w, self.h = width, height
        self.rng = random.Random(seed)
        y, x0 = height // 2, width // 2
        self.body = [(x0 - i, y) for i in range(length)]
        self.heading = "right"
        self.alive, self.eaten, self.steps = True, 0, 0
        self.food: tuple | None = None
        self._spawn()

    def _spawn(self) -> None:
        taken = set(self.body)
        free = [(x, y) for y in range(self.h) for x in range(self.w) if (x, y) not in taken]
        self.food = self.rng.choice(free) if free else None

    @property
    def over(self) -> bool:
        return not self.alive or self.food is None

    def state(self) -> dict:
        return {"width": self.w, "height": self.h, "body": [list(c) for c in self.body],
                "food": list(self.food) if self.food else None, "heading": self.heading}

    def step(self, d: str) -> None:
        self.steps += 1
        body, eats = moved(self.w, self.h, self.body, self.food, d)
        if body is None:
            self.alive = False
            return
        self.body, self.heading = body, d
        if eats:
            self.eaten += 1
            self._spawn()
