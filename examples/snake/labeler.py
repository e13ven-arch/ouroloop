"""The environment's label of a move (DEMO_PLAN §2.2), written as an outcome with source "env". A BFS lookahead that
only the environment's code runs: the label does not depend on how well any model, or the rule snake, plays.

    label(state) -> the move to learn, or None

1. Safe moves: the legal moves that do not die at once. With none the snake is trapped: every legal move dies, no
   move is right, and the decision gets an outcome with label None, which no training or calibration reads.
2. Progress: for each safe move, a shortest path from the new head to the food (BFS; the body blocks, except its
   tail, which moves on). A virtual snake follows it, and the move is good if the snake gets there alive and can then
   still reach its own tail (or is shorter than 4): eating does not shut it in. Eating the last food fills the board
   and wins the game, so that is good too. Its distance is 1 + the path's length.
3. The label is the good move with the shortest distance.
4. No good move (no safe route to the food for now): among the safe moves after which the head can still reach the
   tail (all safe moves if none can), the one that leaves the head the most cells it can reach.

Ties, in 3 and 4: keep the heading if it is among them, else the first of up, down, left, right (game.first). The
rule is a fixed function of the board, so a model can learn it exactly.
"""
from __future__ import annotations

from collections import deque

from .game import DELTA, DIRS, first, legal, moved, unpack


def _neighbours(w: int, h: int, c: tuple):
    for d in DIRS:
        dx, dy = DELTA[d]
        n = (c[0] + dx, c[1] + dy)
        if 0 <= n[0] < w and 0 <= n[1] < h:
            yield d, n


def path(w: int, h: int, body: list[tuple], goal: tuple) -> list[str] | None:
    """The moves of a shortest path from the head to `goal`; the body blocks, except its tail."""
    start, blocked = body[0], set(body[:-1])
    prev: dict = {start: None}
    queue = deque([start])
    while queue:
        c = queue.popleft()
        if c == goal:
            out = []
            while prev[c] is not None:
                c, d = prev[c]
                out.append(d)
            return out[::-1]
        for d, n in _neighbours(w, h, c):
            if n not in prev and n not in blocked:
                prev[n] = (c, d)
                queue.append(n)
    return None


def reach(w: int, h: int, body: list[tuple]) -> set:
    """The cells the head can reach, itself included; the body blocks, except its tail."""
    blocked, seen = set(body[1:-1]), {body[0]}
    stack = [body[0]]
    while stack:
        for _, n in _neighbours(w, h, stack.pop()):
            if n not in seen and n not in blocked:
                seen.add(n)
                stack.append(n)
    return seen


def tail_reachable(w: int, h: int, body: list[tuple]) -> bool:
    return len(body) < 4 or body[-1] in reach(w, h, body)


def label(state: dict) -> str | None:
    w, h, body, food, heading = unpack(state)
    after = {}
    for d in legal(state):
        b, eats = moved(w, h, body, food, d)
        if b is not None:
            after[d] = (b, eats)
    if not after:
        return None
    good = {}
    for d, (b, eats) in after.items():
        route = [] if eats or food is None else path(w, h, b, food)
        if route is None:
            continue
        v = b
        for step in route:
            v, _ = moved(w, h, v, food, step)
            if v is None:
                break
        if v is not None and (len(v) == w * h or tail_reachable(w, h, v)):     # a full board: the game is won
            good[d] = 1 + len(route)
    if good:
        best = min(good.values())
        return first([d for d, n in good.items() if n == best], heading)
    pool = [d for d, (b, _) in after.items() if tail_reachable(w, h, b)] or list(after)
    room = {d: len(reach(w, h, after[d][0])) for d in pool}
    return first([d for d in pool if room[d] == max(room.values())], heading)
