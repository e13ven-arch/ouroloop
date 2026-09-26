"""The Snake scenario pack as ouroloop runs it: the move decision point, and the game runner that `ouroloop demo snake`
calls (ouroloop/generations.py). One game is one ouroloop session."""
from __future__ import annotations

from ouroloop.decision import Choice, DecisionPoint

from .game import Game, legal, render
from .labeler import label

QUESTION = "Which way should the snake move?"
RULE = "rule:examples.snake.rules:keep_going"


def moves(state: dict) -> dict:
    """The candidates: the legal moves (at most three), with no description."""
    return {d: "" for d in legal(state)}


def board(state: dict, params: dict) -> str:
    """The view: the rendered board, under a header with the snake's length and heading."""
    return render(state)


def move_point(**kw) -> DecisionPoint:
    """Choose one of the legal moves. Wherever the model does not act, the rule snake does (there is no fixed label a
    move could fall back to: which moves are legal depends on the heading), and the model acts only on a move other
    than the rule snake's."""
    kw.setdefault("phases", {"move": {"default": RULE}})
    return Choice("move", QUESTION, moves, view=board, **kw)


class Snake:
    """Games on an n x n board: `board`, and `board_after` from generation `change_at` on (the environment change).
    A game ends at death, on a full board, after `max_steps` moves (default 4 n^2) or after `hunger` moves without
    food (default 2 n^2): a snake that circles safely forever ends alive."""

    def __init__(self, board: int = 8, length: int = 3, change_at: int | None = None, board_after: int | None = None,
                 max_steps: int | None = None, hunger: int | None = None):
        self.board, self.length, self.change_at, self.board_after = board, length, change_at, board_after
        self.max_steps, self.hunger = max_steps, hunger

    def size(self, generation: int) -> int:
        changed = self.change_at is not None and self.board_after and generation >= self.change_at
        return self.board_after if changed else self.board

    def play(self, rt, session: str, seed: int, generation: int) -> dict:
        """One game with the release current at its start; every move is labelled by the environment."""
        n = self.size(generation)
        game = Game(n, n, self.length, seed)
        rt.session(session, {"task": "snake", "board": f"{n}x{n}", "seed": seed, "generation": generation})
        cap, hunger, idle = self.max_steps or 4 * n * n, self.hunger or 2 * n * n, 0
        while not game.over and game.steps < cap and idle < hunger:
            state = game.state()
            d = rt.ask("move", state, session=session)
            right = label(state)
            rt.outcome(d.id, right, source="env", **({} if right else {"note": "trapped: every legal move dies"}))
            eaten = game.eaten
            game.step(d.action or game.heading)             # no action (the rule failed): keep going
            idle = 0 if game.eaten > eaten else idle + 1
        return {"board": f"{n}x{n}", "food": game.eaten, "steps": game.steps, "died": not game.alive}
