"""The rule snake, the move point's default answerer ("rule:examples.snake.rules:greedy_safe"): it answers wherever the
model does not act. And the key of the notebook version's tiny model (the signature trainer)."""
from __future__ import annotations

from .game import DELTA, DIRS, first, legal, moved, unpack


def greedy_safe(state: dict) -> str:
    """Toward the food, never into an immediate death when a legal move avoids it: among the safe moves, those that
    bring the head closer to the food (Manhattan distance), else any safe move; ties by game.first. It does not look
    further ahead, so it shuts itself in sooner or later. Trapped (every move dies), it keeps its heading."""
    w, h, body, food, heading = unpack(state)
    safe = [d for d in legal(state) if moved(w, h, body, food, d)[0] is not None]
    if not safe:
        return heading
    if food is not None:
        def dist(d: str) -> int:
            (x, y), (dx, dy) = body[0], DELTA[d]
            return abs(x + dx - food[0]) + abs(y + dy - food[1])
        now = abs(body[0][0] - food[0]) + abs(body[0][1] - food[1])
        closer = [d for d in safe if dist(d) < now]
        if closer:
            return first(closer, heading)
    return first(safe, heading)


def signature(view: str) -> str:
    """The key the signature trainer counts labels by, read from the board text: the heading, what lies next to the
    head in each direction ("." free, the tail included since it moves on; "#" wall or body; "F" food) and the sign
    of the food's offset from the head. What a tiny model can learn: the local picture, no lookahead. It is all the
    rule snake reads, so a model keyed by it can at most copy the rule snake where the labels agree with it."""
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
