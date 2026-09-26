"""The Snake scenario pack (examples/snake) and the multi-generation demo loop that runs it (ouroloop/generations.py),
offline: the signature trainer stands in for TDE's, and a fake TDE backend for the general checkpoint."""
import json
import random
import re
from dataclasses import replace

import pytest

from examples.snake.game import Game, legal, moved, render, unpack
from examples.snake.labeler import label, path
from examples.snake.rules import keep_going, signature
from examples.snake.scenario import QUESTION, RULE, move_point, moves
from ouroloop import config
from ouroloop.backends import TdeBackend
from ouroloop.decision import Choice
from ouroloop.evolve import experience, monitor
from ouroloop.evolve.gate import records
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.replay import right
from ouroloop.evolve.train import SignatureBackend, SignatureTrainer
from ouroloop.generations import _line, run
from ouroloop.ledger import Ledger, SessionStore
from ouroloop.policy import Policy
from ouroloop.release import revoke
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
            game.step(keep_going(game.state()) if rng.random() < 0.8 else rng.choice(legal(game.state())))
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
    long = state([(5, 1), (4, 1), (3, 1), (2, 1), (1, 1), (0, 1), (0, 2), (0, 3), (0, 4), (1, 4), (1, 5)], (0, 0),
                 "right", n=6)
    assert render(long).split("\n")[:3] == ["Length 11. Heading right.", " F . . . . .", " 6 7 8 9 9 H"]  # 9: or more
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
def test_the_label_is_every_best_move_in_dirs_order_and_none_when_trapped():
    assert label(state([(4, 4), (3, 4), (2, 4)], (6, 4), "right")) == ["right"]    # straight at the food
    assert label(state([(4, 4), (3, 4), (2, 4)], (5, 3), "right")) == ["up", "right"]   # as short either way: both
    assert label(state([(4, 4), (4, 5), (4, 6)], (5, 3), "up")) == ["up", "right"]
    assert label(state([(4, 4), (4, 5), (4, 6)], (3, 5), "up")) == ["left"]
    trapped = state([(0, 0), (0, 1), (1, 1), (1, 0), (2, 0)], (7, 7), "up")      # the wall, and its own body
    assert [d for d in legal(trapped) if moved(*unpack(trapped)[:4], d)[0] is not None] == [] and label(trapped) is None
    # the food in a pocket the snake could not leave after eating: the label keeps it out, the rule snake goes in
    pocket = state([(1, 0), (1, 1), (0, 1), (0, 2), (0, 3), (1, 3), (2, 3)], (0, 0), "up", n=6)
    assert (label(pocket), keep_going(pocket)) == (["right"], "left")
    for s in states():
        w, h, body, food, _ = unpack(s)
        safe = [d for d in legal(s) if moved(w, h, body, food, d)[0] is not None]
        good = label(s)
        assert (good is None) == (not safe) and (not safe or good == [d for d in safe if d in good])


def test_the_label_on_hand_built_boards_a_dead_end_food_behind_the_tail_and_a_full_board():
    # a dead end: the food lies in the two top rows, which the body closes off; the snake could get to it but never
    # out again. No move is good, so the label keeps the tail in reach; the rule snake turns in
    dead_end = state([(5, 2), (4, 2), (3, 2), (2, 2), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3), (3, 3), (3, 4), (2, 4),
                      (1, 4), (0, 4)], (2, 0), "right", n=6)
    assert (label(dead_end), keep_going(dead_end)) == (["down"], "up")
    # the food behind the tail: the only way to it is through the cell the tail leaves (blocked, there is none)
    behind = state([(3, 0), (2, 0), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (1, 5)], (0, 3), "right", n=6)
    w, h, body, food, _ = unpack(behind)
    after, _ = moved(w, h, body, food, "down")
    route = path(w, h, after, food)
    assert label(behind) == ["down"] and route is not None and path(w, h, [*after, after[-1]], food) is None
    # a full board: eating the last food wins the game, although the tail is out of reach afterwards (3 x 3 has no
    # cycle through every cell); the other safe move, into the tail's cell, only goes round
    full = {"width": 3, "height": 3, "body": [[1, 0], [1, 1], [2, 1], [2, 2], [1, 2], [0, 2], [0, 1], [0, 0]],
            "food": [2, 0], "heading": "up"}
    assert label(full) == ["right"]
    game = Game(3, 3, 3, seed=0)
    game.body, game.heading, game.food = [tuple(c) for c in full["body"]], "up", (2, 0)
    game.step(label(full)[0])
    assert game.over and game.alive and len(game.body) == 9


def test_the_rule_snake_keeps_going_and_only_turns_to_avoid_dying():
    assert keep_going(state([(4, 4), (3, 4), (2, 4)], (4, 1), "right")) == "right"     # the food does not turn it
    assert keep_going(state([(7, 4), (6, 4), (5, 4)], (4, 1), "right")) == "up"        # the wall: the first safe
    assert keep_going(state([(7, 0), (6, 0), (5, 0)], (4, 1), "right")) == "down"
    trapped = state([(0, 0), (0, 1), (1, 1), (1, 0), (2, 0)], (7, 7), "up")
    assert keep_going(trapped) == "up"
    for s in states(seed=1):
        w, h, body, food, heading = unpack(s)
        safe = [d for d in legal(s) if moved(w, h, body, food, d)[0] is not None]
        assert keep_going(s) == (heading if heading in safe else safe[0] if safe else heading)


def test_the_tiny_models_key_is_the_local_picture():
    s = state([(4, 2), (5, 2), (5, 3), (4, 3)], (1, 1), "left")
    assert signature(render(s)) == "left ...# --"            # up, down (its tail: it moves on), left free; the neck
    assert signature(render(state([(0, 0), (1, 0), (2, 0)], None, "left"))) == "left #.## "   # walls; no food


# ---------------------------------------------------------------- the decision point
def test_the_move_point_asks_the_legal_moves_and_the_rule_snake_answers_until_a_model_may_act(tmp_path):
    rt = Runtime(tmp_path, {"base": FakeTde("general")}, "base", seed=0)
    point = rt.register(move_point(policy=Policy(act=True)))
    assert point.default_for() == ("move", None, RULE)
    s = state([(4, 4), (4, 5), (4, 6)], (4, 1), "up")
    d = rt.ask("move", s, session="g1")
    row = rt.ledger.rows()[-1].decision
    assert sorted(row["candidates"]) == ["left", "right", "up"] and row["view"] == render(s)
    assert (d.route, d.action, row["answerer"], row["default"]) == ("default", keep_going(s), RULE, keep_going(s))


# ---------------------------------------------------------------- the demo loop
class Uniform:
    """A fake TDE decider: it knows nothing of Snake."""

    def decide_batch(self, state, questions):
        return {k: {"probabilities": {c: 1.0 for c in q["criteria"]}} for k, q in questions.items()}


class FakeTde(TdeBackend):
    """The tde backend, with a fake decider in place of the general checkpoint."""

    def __init__(self, run, model="tde:general"):
        super().__init__(run, decider=Uniform(), model=model)


def seen(state: dict, params: dict) -> str:
    """The board, and the state it was drawn from."""
    return f"{render(state)}\n{json.dumps(state)}"


def seeing_move_point(**kw):
    """The move point with a view the whole state can be read from."""
    kw.setdefault("phases", {"move": {"default": RULE}})
    return Choice("move", QUESTION, moves, view=seen, **kw)


def sees(view: str) -> str:
    """The signature trainer's key: the environment's right moves and the legal moves, read from the state in the
    view. A model keyed by it learns what the labeler knows, where the rule snake goes wrong too (TDE's part on a GPU
    host), and has nothing to pick among several right moves. Trained on shuffled labels it learns nothing: within a
    key every candidate is always there, so a label shuffled in lands on each alike."""
    try:
        state = json.loads(view.rsplit("\n", 1)[-1])
    except ValueError:                  # not a board (the model id probe)
        return "other"
    return f"{' '.join(label(state) or ['trapped'])} | {' '.join(legal(state))}"


class Seen(SignatureBackend):
    """The signature model, abstaining on a key it was not trained on: its fallback, the label counts over every key,
    would otherwise act on a guess where the champion's games lead the snake somewhere the defaults' games did not."""

    def decide(self, requests):
        answers = super().decide(requests)
        for r, a in zip(requests, answers):
            a.abstained = self.signature(r.state) not in self.table.get(r.question.instructions, {})
        return answers


class SeenTrainer(SignatureTrainer):
    """The signature trainer, its models served by Seen."""

    def train(self, job):
        result = super().train(job)
        return replace(result, backend={**result.backend, "type": "test_snake:Seen"})

    def backend(self, result):
        return Seen(result.backend["path"], signature=result.backend.get("signature"))


# Generation 1 leaves too few training rows beside its 30 test games (the data checks stop it before training);
# generation 2 trains the first candidate; generation 3's is planted.
CFG = {"workspace": {"root": "ws"}, "seed": 0,
       "demo": {"scenario": "examples.snake.scenario:Snake", "games": 40, "plant": 3, "seed": 0,
                "options": {"board": 6, "max_steps": 60},
                "gate": {"risk": 0.2, "min_cal_sessions": 10, "cal_frac": {"move": 0.5, "*": 0.15},
                         "checks": {"min_train": 400, "max_mde": 0.5}}},
       "backends": {"general": {"type": "test_snake:FakeTde", "run": "~/tde/release/tde-general-v0.1"}},
       "registry": {"base": "general"},
       "points": {"default_backend": "general",
                  "move": {"define": "test_snake:seeing_move_point", "backend": "champion", "act": True}},
       "trainer": {"type": "test_snake:SeenTrainer", "signature": "test_snake:sees"},
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
    # generation 1: too few training rows, nothing trained; generation 2: the first candidate, promoted
    assert cards[0]["gate"] == "insufficient_data" and "model_id" not in cards[0]
    assert cards[1]["gate"] == "accepted" and cards[1]["metrics"]["move"]["challenger"]["fixes"] > 0
    # the planted candidate, trained on shuffled labels, went through the same gate and was rejected
    planted = cards[2]
    assert planted["planted"] == "shuffled labels" and planted["gate"] == "rejected"
    history = json.loads((root / "models" / "champion.json").read_text())["history"]
    assert "m_0003" not in [h["version"] for h in history]
    assert [e.get("planted") for e in experience.read(root)] == [None, None, "shuffled labels"]
    # a generation's games are played by the champion the previous generation's round left: its model id is on them
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
    assert champion_after[2] == ("m_0002", "signature:m_0002")
    # every round's gate scored fresh games only: exactly the generation just played, never gated before, never
    # trained or calibrated on by its candidate; the first round stopped before inference and used nothing
    first, *used = records(root)
    assert not first.used and [r.used for r in used] == [True, True]
    seen = set()
    for g, record in zip((2, 3), used):
        games = sorted(s for s in sessions if sessions[s].get("generation") == g)
        assert record.fresh and not seen & set(record.test_sessions)
        assert set(record.test_sessions) <= set(games) and len(record.test_sessions) == (30 if g == 2 else 40)
        data = root / "models" / f"m_{g:04d}" / "data"
        trained = {json.loads(line)["source_id"] for p in data.glob("*.jsonl") for line in p.open()}
        assert trained and not trained & set(record.test_sessions)
        seen |= set(record.test_sessions)
    assert len(lines) == 2 + 3 + 2 and lines[4].split()[:2] == ["3", "m_0002"] and "planted" in lines[4]


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


def metrics_of(ledger, generation):
    return {e["session"]: e["metrics"] for e in ledger.events()
            if e.get("type") == "metrics" and e["generation"] == generation}


@pytest.mark.usefixtures("loose_risk")
def test_every_printed_number_is_on_disk_beside_the_same_games_played_by_the_defaults(tmp_path):
    """DEMO_PLAN §0.2: every number the demo prints comes from this run and can be checked on disk; here each is
    recomputed from the workspace alone. And each generation's games are played again by the defaults alone (the
    rule snake: no model is asked), on the same seeds and board, in baseline/, and printed beside them: a bigger
    board, other seeds or luck cannot pass for what the model did."""
    lines = []
    root = run(CFG, tmp_path, generations=3, out=lines.append)
    rt = config.build_runtime(CFG, tmp_path)
    base, base_contexts = Ledger(root / "baseline" / "ledger"), SessionStore(root / "baseline" / "sessions")
    sessions, _ = rt.ledger.sessions()
    base_sessions, _ = base.sessions()
    summaries = [e for e in rt.ledger.events() if e.get("type") == "generation"]
    assert [e["generation"] for e in summaries] == [1, 2, 3]
    for g, line, summary in zip((1, 2, 3), lines[2:5], summaries):
        mine = sorted(s for s, info in sessions.items() if info.get("generation") == g)
        rows = [r.decision for r in rt.ledger.rows("move") if r.decision["session"] in mine]
        games, again = metrics_of(rt.ledger, g), metrics_of(base, g)
        assert sorted(games) == sorted(again) == mine
        for s in mine:              # a game's metrics are its decisions': a move a step, the food, a fatal last move
            moves = [d for d in rows if d["session"] == s]
            first, last = rt.sessions.get(moves[0]["ctx"]), rt.sessions.get(moves[-1]["ctx"])
            w, h, body, food, _ = unpack(last)
            after, eats = moved(w, h, body, food, moves[-1]["action"])
            assert (games[s]["steps"], games[s]["died"]) == (len(moves), after is None)
            assert games[s]["food"] == len(body) - len(first["body"]) + eats
            assert {k: base_sessions[s][k] for k in ("board", "seed")} == {k: sessions[s][k] for k in ("board", "seed")}
        base_rows = [r.decision for r in base.rows("move") if r.decision["session"] in mine]
        assert base_rows and all(d["route"] == "shadow" and d["backend"] == "none" and
                                 d["action"] == keep_going(base_contexts.get(d["ctx"])) for d in base_rows[:500])
        played = {sessions[s]["champion"] or "base" for s in mine}
        acted = sum(d["route"] == "act" for d in rows)
        # each labelled act: (right, the default was right too: a swap between right moves, no fix)
        judged = [(r.decision["action"] in right(t[0]), r.decision["default"] in right(t[0]))
                  for r in rt.ledger.rows("move")
                  if r.decision["session"] in mine and r.decision["route"] == "act" and (t := r.target())]
        fixes, harms = sum(ok and not same for ok, same in judged), sum(not ok for ok, _ in judged)
        bad, n = monitor.realized(rt, "move", set(mine))
        card = Registry(root).card(f"m_{g:04d}")
        assert (summary["played"], summary["sessions"], summary["decisions"], summary["acted"], summary["fixes"],
                summary["harms"], summary["harmed"], summary["acted_sessions"]) == (
            sorted(played), len(mine), len(rows), acted, fixes, harms, bad, n)
        assert (summary["candidate"], summary["gate"]) == (card["version"], card["gate"])
        mean = {k: sum(m[k] for m in games.values()) / len(games) for k in ("food", "steps")}
        base_mean = {k: sum(m[k] for m in again.values()) / len(again) for k in ("food", "steps")}
        died, base_died = sum(m["died"] for m in games.values()), sum(m["died"] for m in again.values())
        assert line.split()[:3] == [str(g), ",".join(sorted(played)), f"{acted / len(rows):.0%}"]
        for piece in [f"{bad}/{n}=" if n else " - ", f" {(fixes - harms) / len(mine):+.2f} ",
                      f"food={mean['food']:.1f} ({base_mean['food']:.1f})",
                      f"steps={mean['steps']:.1f} ({base_mean['steps']:.1f})",
                      f"died={died}/{len(mine)} ({base_died}/{len(mine)})", "board=6x6 ",
                      f"{card['version']} {card['gate']}"]:
            assert piece in line, (piece, line)
        if g == 3:                  # the champion acts where the rule snake goes wrong, and harms no game
            assert acted and fixes and not harms
        if g == 1:                  # nothing has been promoted: no model acts, and the games are the defaults' games
            assert acted == 0 and games == again


@pytest.mark.usefixtures("loose_risk")
def test_the_games_of_a_revoked_champion_say_so_and_its_revocation_trains_again(tmp_path):
    cfg = {**CFG, "demo": {**CFG["demo"], "plant": None}}
    root = run(cfg, tmp_path, generations=2, out=lambda line: None)
    champion = Registry(root).champion()
    revoke(root, champion["version"], champion["model_id"], "a test")
    lines = []
    run(cfg, tmp_path, generations=1, out=lines.append)
    assert lines[2].startswith(f"  3  {champion['version']} (revoked)") and lines[2].split()[3] == "0%"
    assert "skipped" not in lines[2] and Registry(root).versions()[-1] == "m_0003"


@pytest.mark.usefixtures("loose_risk")
def test_the_planted_candidate_is_rejected_for_its_labels_and_the_pooled_line_covers_every_generation(tmp_path):
    """The same round without the plant: the same gate on the same test games accepts the candidate its real labels
    train, so the planted one fails for its labels, nothing else. And a workspace continued by a second run pools
    every generation on disk, not only the second run's."""
    real = {**CFG, "demo": {**CFG["demo"], "plant": None}}
    planted = {**CFG, "demo": {**CFG["demo"], "plant": 2}}
    lines = []
    root = run(real, tmp_path / "real", generations=2, out=lines.append)
    planted = run(planted, tmp_path / "planted", generations=2, out=lambda line: None)
    (a, b), (c, d) = ([Registry(r).card(v) for v in ("m_0001", "m_0002")] for r in (root, planted))
    assert a["test_sessions"] == c["test_sessions"] and b["test_sessions"] == d["test_sessions"]
    assert (b["gate"], d["gate"], d["reasons"]) == ("accepted", "rejected", ["no point's gain rose significantly"])
    assert b["metrics"]["move"]["champion"] == d["metrics"]["move"]["champion"]
    assert d["metrics"]["move"]["challenger"]["fixes"] < b["metrics"]["move"]["challenger"]["fixes"]
    # the promoted model fixes the target share of the default errors: the next rounds train nothing
    run(real, tmp_path / "real", generations=2, out=lines.append)
    summaries = [e for e in config.build_runtime(real, tmp_path / "real").ledger.events()
                 if e.get("type") == "generation"]
    assert [e["generation"] for e in summaries] == [1, 2, 3, 4]
    assert all(e["candidate"] is None and e["skipped"].startswith("target reached: m_0002") for e in summaries[2:])
    assert Registry(root).versions() == ["m_0001", "m_0002"] and summaries[3]["acted_sessions"]
    bad, n = sum(e["harmed"] for e in summaries), sum(e["acted_sessions"] for e in summaries)
    assert lines[-2].startswith(f"all 4 generations: harmed sessions {bad}/{n}=")
    header, first = lines[1], lines[2]          # the gain sits under its header: 57 characters of columns before it
    assert header[57:62] == " gain" and re.fullmatch(r" *([+-]\d\.\d\d|-)", first[57:62]) and first[62:64] == "  "


def test_a_round_that_ran_and_failed_is_not_reported_as_skipped():
    e = {"generation": 2, "played": ["m_0001"], "decisions": 10, "acted": 1, "fixes": 1, "harms": 0, "sessions": 5,
         "harmed": 0, "acted_sessions": 1, "risk": 0.05, "metrics": {}, "baseline": None, "candidate": None,
         "gate": "inconclusive", "skipped": None, "champion": "m_0001", "planted": False, "revoked": []}
    assert _line(e).endswith("|  - inconclusive")
    assert _line({**e, "gate": None, "skipped": "target reached"}).endswith("|  skipped: target reached")
