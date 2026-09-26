"""The rule snake, the move point's default answerer ("rule:examples.snake.rules:keep_going"): it answers wherever the
model does not act. And the key of the notebook version's tiny model (the signature trainer)."""
from __future__ import annotations

from .game import DELTA, DIRS, first, legal, moved, unpack


def keep_going(state: dict) -> str:
    """A naive snake that keeps going and only turns to avoid dying: the heading if moving on does not die at once,
    else the first safe move in DIRS order, else (trapped) the heading. It never heads for the food."""
    w, h, body, food, heading = unpack(state)
    safe = [d for d in legal(state) if moved(w, h, body, food, d)[0] is not None]
    return first(safe, heading) if safe else heading


def signature(view: str) -> str:
    """The key the signature trainer counts labels by, read from the board text: the heading, what lies next to the
    head in each direction ("." free, the tail included since it moves on; "#" wall or body; "F" food) and the sign
    of the food's offset from the head. What a tiny model can learn: the local picture, no lookahead. The rule snake
    reads less (not the food), so a model keyed by it can learn to head for the food where the rule snake keeps
    going, but never to see a dead end coming."""
    lines = view.split("\n")
    heading = lines[0].rstrip(".").rsplit(" ", 1)[-1]
    grid = [row[1::2] for row in lines[1:]]           # every cell is " X"
    cells = {(x, y): c for y, row in enumerate(grid) for x, c in enumerate(row)}
    head = next((p for p, c in cells.items() if c == "H"), None)
    food = next((p for p, c in cells.items() if c == "F"), None)
    if head is None:
        return "other"
    near = ""
    for d in DIRS:
        c = cells.get((head[0] + DELTA[d][0], head[1] + DELTA[d][1]), "#")
        near += "." if c in ".1" else "F" if c == "F" else "#"
    offset = "" if food is None else "".join("-0+"[(a > b) - (a < b) + 1] for a, b in zip(food, head))
    return f"{heading} {near} {offset}"
