"""The Snake scenario pack (examples/snake) and the multi-generation demo loop that runs it (ouroloop/generations.py),
offline: the signature trainer stands in for TDE's, and a fake TDE backend for the general checkpoint."""
import json
import random

import pytest

from examples.snake.game import Game, legal, moved, render, unpack
from examples.snake.labeler import label, path
from examples.snake.rules import greedy_safe, signature
from examples.snake.scenario import RULE, move_point
from ouroloop import config
from ouroloop.backends import TdeBackend
from ouroloop.evolve import experience
from ouroloop.evolve.gate import records
from ouroloop.evolve.registry import Registry
from ouroloop.generations import run
from ouroloop.policy import Policy
from ouroloop.runtime import Runtime


def state(body, food, heading, n=8):
    return {"width": n, "height": n, "body": [list(c) for c in body], "food": list(food) if food else None,
            "heading": heading}


def states(n_games=30, board=8, seed=0):
    """States from games the rule snake plays."""
    out, rng = [], random.Random(seed)
    for g in range(n_games):
        game = Game(board, board, 3, rng.randrange(10 ** 6))
        while not game.over and game.steps < 300:
            out.append(game.state())
            game.step(greedy_safe(game.state()) if rng.random() < 0.8 else rng.choice(legal(game.state())))
    return out


# ---------------------------------------------------------------- the game
def test_the_board_text_is_the_jev_integrations():
    s = state([(4, 2), (5, 2), (5, 3), (5, 4)], (1, 1), "left")
    assert render(s) == "\n".join(["Length 4. Heading left.",
                                   " . . . . . . . .",
                                   " . F . . . . . .",
                                   " . . . . H 3 . .",
                                   " . . . . . 2 . .",
                                   " . . . . . 1 . .",
                                   " . . . . . . . .",
                                   " . . . . . . . .",
                                   " . . . . . . . ."])
    assert Game(8, 8, 3, seed=1).state()["body"] == [[4, 4], [3, 4], [2, 4]]     # the middle, heading right


def test_the_rules():
    s = state([(4, 2), (5, 2), (5, 3), (4, 3)], (0, 0), "left")
    w, h, body, food, _ = unpack(s)
    assert legal(s) == ["up", "down", "left"]                                    # never back into the neck
    assert moved(w, h, body, food, "down") == ([(4, 3), (4, 2), (5, 2), (5, 3)], False)   # the tail moves on
    assert moved(w, h, [(0, 0), (1, 0)], food, "up") == (None, False)                    # the wall
    assert moved(w, h, [(4, 2), (5, 2), (5, 3), (4, 3), (3, 3)], food, "down") == (None, False)   # the body
    game = Game(8, 8, 3, seed=0)
    game.food = (5, 4)
    game.step("right")
    assert (game.eaten, len(game.body), game.alive) == (1, 4, True) and game.food not in game.body
    game.step("left")
    assert not game.alive                                                         # back into its neck


# ---------------------------------------------------------------- the environment's label and the rule snake
def test_the_label_is_safe_progress_with_documented_ties_and_none_when_trapped():
    assert label(state([(4, 4), (3, 4), (2, 4)], (6, 4), "right")) == "right"      # straight at the food
    assert label(state([(4, 4), (3, 4), (2, 4)], (5, 3), "right")) == "right"      # a tie: the heading
    assert label(state([(4, 4), (4, 5), (4, 6)], (5, 3), "up")) == "up"
    assert label(state([(4, 4), (4, 5), (4, 6)], (3, 5), "up")) == "left"          # a tie without it: DIRS order
    trapped = state([(0, 0), (0, 1), (1, 1), (1, 0), (2, 0)], (7, 7), "up")      # the wall, and its own body
    assert [d for d in legal(trapped) if moved(*unpack(trapped)[:4], d)[0] is not None] == [] and label(trapped) is None
    # the food in a pocket the snake could not leave after eating: the label keeps it out, the rule snake goes in
    pocket = state([(1, 0), (1, 1), (0, 1), (0, 2), (0, 3), (1, 3), (2, 3)], (0, 0), "up", n=6)
    assert (label(pocket), greedy_safe(pocket)) == ("right", "left")
    for s in states():
        w, h, body, food, _ = unpack(s)
        safe = [d for d in legal(s) if moved(w, h, body, food, d)[0] is not None]
        assert (label(s) is None) == (not safe) and (label(s) in safe or not safe)


def test_the_label_on_hand_built_boards_a_dead_end_food_behind_the_tail_and_a_full_board():
    # a dead end: the food lies in the two top rows, which the body closes off; the snake could get to it but never
    # out again. No move is good, so the label keeps the tail in reach; the rule snake heads in
    dead_end = state([(5, 2), (4, 2), (3, 2), (2, 2), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3), (3, 3), (3, 4), (2, 4),
                      (1, 4), (0, 4)], (2, 0), "right", n=6)
    assert (label(dead_end), greedy_safe(dead_end)) == ("down", "up")
    # the food behind the tail: the only way to it is through the cell the tail leaves (blocked, there is none)
    behind = state([(3, 0), (2, 0), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (1, 5)], (0, 3), "right", n=6)
    w, h, body, food, _ = unpack(behind)
    after, _ = moved(w, h, body, food, "down")
    route = path(w, h, after, food)
    assert label(behind) == "down" and route is not None and path(w, h, [*after, after[-1]], food) is None
    # a full board: eating the last food wins the game, although the tail is out of reach afterwards (3 x 3 has no
    # cycle through every cell); the other safe move, into the tail's cell, only goes round
    full = {"width": 3, "height": 3, "body": [[1, 0], [1, 1], [2, 1], [2, 2], [1, 2], [0, 2], [0, 1], [0, 0]],
            "food": [2, 0], "heading": "up"}
    assert label(full) == "right"
    game = Game(3, 3, 3, seed=0)
    game.body, game.heading, game.food = [tuple(c) for c in full["body"]], "up", (2, 0)
    game.step(label(full))
    assert game.over and game.alive and len(game.body) == 9


def test_the_rule_snake_heads_for_the_food_and_never_dies_when_it_can_help_it():
    assert greedy_safe(state([(4, 4), (3, 4), (2, 4)], (4, 1), "right")) == "up"
    for s in states(seed=1):
        w, h, body, food, heading = unpack(s)
        safe = [d for d in legal(s) if moved(w, h, body, food, d)[0] is not None]
        assert greedy_safe(s) in (safe or [heading])


def test_the_tiny_models_key_is_the_local_picture():
    s = state([(4, 2), (5, 2), (5, 3), (4, 3)], (1, 1), "left")
    assert signature(render(s)) == "left ...# --"            # up, down (its tail: it moves on), left free; the neck
    assert signature(render(state([(0, 0), (1, 0), (2, 0)], None, "left"))) == "left #.## "   # walls; no food


# ---------------------------------------------------------------- the decision point
def test_the_move_point_asks_the_legal_moves_and_the_rule_snake_answers_until_a_model_may_act(tmp_path):
    rt = Runtime(tmp_path, {"base": FakeTde("general")}, "base", seed=0)
    point = rt.register(move_point(policy=Policy(act=True)))
    assert point.count == "sampled" and point.default_for() == ("move", None, RULE)
    s = state([(4, 4), (4, 5), (4, 6)], (4, 1), "up")
    d = rt.ask("move", s, session="g1")
    row = rt.ledger.rows()[-1].decision
    assert sorted(row["candidates"]) == ["left", "right", "up"] and row["view"] == render(s)
    assert (d.route, d.action, row["answerer"], row["default"]) == ("default", greedy_safe(s), RULE, None)


# ---------------------------------------------------------------- the demo loop
class Uniform:
    """A fake TDE decider: it knows nothing of Snake."""

    def decide_batch(self, state, questions):
        return {k: {"probabilities": {c: 1.0 for c in q["criteria"]}} for k, q in questions.items()}


class FakeTde(TdeBackend):
    """The tde backend, with a fake decider in place of the general checkpoint."""

    def __init__(self, run, model="tde:general"):
        super().__init__(run, decider=Uniform(), model=model)


CFG = {"workspace": {"root": "ws"}, "seed": 0,
       "demo": {"scenario": "examples.snake.scenario:Snake", "games": 40, "plant": 2, "seed": 0,
                "options": {"board": 6, "max_steps": 60},
                "gate": {"risk": 0.2, "min_cal_sessions": 10, "cal_frac": {"move": 0.5, "*": 0.15},
                         "checks": {"min_train": 100, "max_mde": 0.5}}},
       "backends": {"general": {"type": "test_snake:FakeTde", "run": "~/tde/release/tde-general-v0.1"}},
       "registry": {"base": "general"},
       "points": {"default_backend": "general",
                  "move": {"define": "examples.snake.scenario:move_point", "backend": "champion", "act": True}},
       "trainer": {"type": "signature", "signature": "examples.snake.rules:signature"},
       "recipe": {"min_weight": 1.0}}


@pytest.mark.usefixtures("loose_risk")
def test_each_generation_learns_from_the_last_ones_model_on_fresh_games_and_the_planted_candidate_fails(tmp_path):
    lines = []
    root = run(CFG, tmp_path, generations=3, out=lines.append)
    rt = config.build_runtime(CFG, tmp_path)
    sessions, _ = rt.ledger.sessions()
    registry = Registry(root)
    cards = [registry.card(v) for v in registry.versions()]
    assert [c["version"] for c in cards] == ["m_0001", "m_0002", "m_0003"]
    # the planted candidate, trained on shuffled labels, went through the same gate and was rejected
    planted = cards[1]
    assert planted["planted"] == "shuffled labels" and planted["gate"] == "rejected"
    history = json.loads((root / "models" / "champion.json").read_text())["history"]
    assert "m_0002" not in [h["version"] for h in history]
    assert [e.get("planted") for e in experience.read(root)] == [None, "shuffled labels", None]
    # a generation's games are played by the champion the previous generation's round left: its model id is on them
    assert cards[0]["gate"] == "accepted"
    champion_after = {0: (None, None)}
    for g, card in enumerate(cards, 1):
        before = champion_after[g - 1]
        champion_after[g] = (card["version"], card["model_id"]) if card["gate"] == "accepted" else before
    for g in (1, 2, 3):
        played = {(sessions[s]["champion"], sessions[s]["release"]["model"]) for s in sessions
                  if sessions[s].get("generation") == g}
        assert played == {champion_after[g - 1]}, g
        assert all(sessions[s]["board"] == "6x6" and "seed" in sessions[s] for s in sessions
                   if sessions[s].get("generation") == g)
    assert champion_after[1] == ("m_0001", "signature:m_0001")
    # every round's gate scored fresh games only: exactly the generation just played, never gated before, never
    # trained or calibrated on by its candidate
    seen = set()
    for g, record in enumerate(records(root), 1):
        games = sorted(s for s in sessions if sessions[s].get("generation") == g)
        assert record.used and record.fresh and not seen & set(record.test_sessions)
        assert set(record.test_sessions) <= set(games) and len(record.test_sessions) == (30 if g == 1 else 40)
        data = root / "models" / f"m_{g:04d}" / "data"
        trained = {json.loads(line)["source_id"] for p in data.glob("*.jsonl") for line in p.open()}
        assert trained and not trained & set(record.test_sessions)
        seen |= set(record.test_sessions)
    assert len(lines) == 2 + 3 + 2 and lines[3].split()[:2] == ["2", "m_0001"] and "planted" in lines[3]


@pytest.mark.usefixtures("loose_risk")
def test_the_demo_command_reads_the_scenarios_config_and_continues_a_workspace(tmp_path, capsys):
    from ouroloop.cli import main
    path = tmp_path / "demo.toml"
    path.write_text("\n".join([
        '[workspace]', 'root = "ws"',
        '[demo]', 'scenario = "examples.snake.scenario:Snake"', 'games = 30', 'plant = 99',
        '[demo.options]', 'board = 5', 'max_steps = 30',
        '[demo.gate]', 'risk = 0.2', 'min_cal_sessions = 10', 'checks = { min_train = 50, max_mde = 0.9 }',
        '[backends.general]', 'type = "test_snake:FakeTde"', 'run = "general"',
        '[registry]', 'base = "general"',
        '[points]', 'default_backend = "general"',
        '[points.move]', 'define = "examples.snake.scenario:move_point"', 'backend = "champion"', 'act = true',
        '[trainer]', 'type = "signature"', 'signature = "examples.snake.rules:signature"']))
    assert main(["demo", "snake", "--config", str(path), "--generations", "1"]) == 0
    assert main(["demo", "snake", "--config", str(path), "--generations", "1"]) == 0
    out = capsys.readouterr().out
    assert [line.split()[0] for line in out.splitlines() if line[:3].strip().isdigit()] == ["1", "2"]
    assert "ledger/" in out and "gates/" in out and "models/" in out
    assert main(["demo", "nowhere"]) == 2
