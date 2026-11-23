# Evaluation: is the coach worth using, and where does it fail?

Phase 4 milestone. Everything here is reproducible from the repository: the games are in
`backend/eval/data/`, the code is `backend/eval/run_eval.py`, and the raw numbers are in
`backend/eval/results/results.json`. Real outputs below were copied from `backend/eval/results/examples.md`.

**Short version.** On a sample where the right answer is known (a weakness planted on purpose):

- The coach found and cited the planted weakness in **8 of 8** recent games. A recent-accuracy summary moved by only
  **-1.6 points** and named nothing.
- Every cited move checked out against the stored evidence (**100%** of 62 claims), and the coach
  declined to compare in every case where fewer than 3 earlier games existed.
- The evaluation also exposed **three real bugs**, now fixed (see [Bugs the evaluation found](#bugs-the-evaluation-found)).
- It did **not** meet one pre-set threshold (runtime ratio against the simplest workflow) and it has **not yet
  measured the model-backed mode**, because no model key was available where this was run.
- No human participants were involved. "Time to find examples" and "usefulness" are measured with stated proxies, not people.

## 1. What was compared

| System | What it is |
| --- | --- |
| **Recent-accuracy baseline** | Average accuracy of the later half of the games against the earlier half. What most review summaries give you. |
| **Usual single-game review** | The three costliest moves of each game, with no memory of other games (what a typical engine review of one game shows). Built from the same Stockfish evidence, so it is the best case for this tool. |
| **This app, deterministic mode** | Dashboard plus the coach review with no model (`LLM_DISABLED=1`). |
| **This app, model-backed mode** | The same workflow with a free hosted model choosing decisions and writing the text. **Not measured yet**, see section 6. |

For the coordination question, three review workflows get the **same evidence tools** (`candidate_decisions`,
`search_history`, `verdict_for`) on the **same games**:

| Workflow | How it works |
| --- | --- |
| **Multi-agent graph** (the product) | Reviewer, history specialist with widening search, narrowing, writer, verifier, study coach. |
| **Fixed workflow** | The top 4 decisions, one exact-position search each, no re-planning. |
| **Single specialist** | One agent that tries every search level and states a verdict from the first one that finds anything, without checking that the match is strong enough. |

## 2. The pilot sample and thresholds (fixed before the first run)

Engine-played games with a planted weakness (`backend/eval/gen_games.py`, seeded). Why synthetic: with real players
"the right answer" is an opinion, so every score would depend on who judged it. All games start 1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6 so that positions genuinely repeat.

| Player | Games | What was planted |
| --- | --- | --- |
| PilotA ("changed") | 24 | 4.Ke2?! (a king walk) in 2 of the first 12 games, then in 8 of the last 12 |
| PilotB ("steady") | 24 | 4.Ke2?! in 2 of the first 12 and 2 of the last 12: nothing changed |
| PilotC ("sparse") | 2 | only two games, so the app should decline to compare |

Analysis used the production settings (Stockfish 18, one thread, 150k nodes, deep checks on critical moments).

Thresholds (also in `THRESHOLDS` in `run_eval.py`):

| # | Measure | Threshold |
| --- | --- | --- |
| 1 | Cited moves that exist, are the player's, and match as claimed | 100% |
| 2 | Verdicts stated from fewer than 3 earlier games | 0 |
| 3 | "Earlier" games that were actually played later | 0 |
| 4 | Planted move picked for review, in planted games | at least 80% |
| 5 | ...and compared with earlier games instead of "judged in this game only" | at least 80% |
| 6 | Recurring-weakness claims about the steady player | 0 |
| 7 | Sparse player: claims that decline to compare | 100% |
| 8 | Recommended study game shows the same weakness | at least 90% |
| 9 | Games a reader opens to find 3 supporting examples: baseline divided by app | at least 3 |
| 10 | Graph runtime divided by fixed-workflow runtime | at most 3 |

## 3. Results

### Against the baselines

| | Recent-accuracy | Usual single-game review | This app (deterministic) |
| --- | --- | --- | --- |
| Accuracy, first half to second half, PilotA | 94.1 to 92.6 (change -1.6) | n/a | n/a |
| Same, PilotB (nothing changed) | 93.8 to 94.2 (change 0.5) | n/a | n/a |
| Names what changed | no | no | yes: king safety in the opening, **recurring** in 10 of 24 games (PilotA) |
| Says it repeats | no | no | yes |
| Planted move shown for a game | no | 8/8 (in the game's top 3 mistakes) | 8/8 (selected and compared) |
| Cites positions from other games | 0 | 0 | yes, as a list of games and moves |
| Games opened to find 3 other examples (PilotA, 8 reviews) | n/a | 4 to 5 (median 4) | 1 (the review lists them) |
| Same, PilotB | n/a | 22 | 1 |
| False alarm on the steady player | no (change +0.5) | n/a | dashboard says **emerging** (4 games), not recurring |

Reading: the accuracy number cannot see this weakness at all, because the king walk costs a few points in a few
games and the rest of each game dilutes it. The usual single-game review does show the move in each game, so
"finding the move" is not what the app adds; what it adds is that the move is **the same one in other games**, with
the games listed, and that it is **not** raised for the steady player.

### Coordination: three workflows, same evidence

62 claims each (26 reviews: the 12 latest games of PilotA and PilotB, and both PilotC games).

| | Multi-agent graph | Fixed workflow | Single specialist |
| --- | --- | --- | --- |
| Evidence accuracy (cited moves valid) | 100% | 100% | 100% |
| Verdicts from fewer than 3 earlier games | **0** | 0 | **4** |
| Planted move selected / compared (PilotA) | 8/8 / 8/8 | 8/8 / 8/8 | 8/8 / 8/8 |
| Sparse player: claims that decline | 3/3 | 3/3 | **2/3** |
| Steady player: claims that give a verdict | 26 of 26 | 5 of 26 (the rest: "not enough evidence") | 26 of 26 |
| Selections that cost at least 5 win-% points | 79% | 79% | 79% |
| Mean time per review (tools only, no engine) | 38 ms | 0.5 ms | 1.4 ms |

Reading:

- **Single specialist** is as good at finding the weakness but **overclaims** (states verdicts from too little evidence)
  and fails to decline on the sparse player. A single agent with the same tools is not safe without a strength check.
- **Fixed workflow** never overclaims but, because it looks only at the exact position, it gives up on 21 of 26 steady-player
  decisions. The graph's widening search makes a comparison for all of them while still never overclaiming.
  This shows more *coverage*; whether the extra comparisons are more *useful* to people has not been tested.
- All three pick the same decisions (the selection step is shared), so the difference lies in how they search and when they stop.
- The graph is about 70 times slower than the fixed workflow. That misses threshold 10. In practice 38 ms is nothing next to
  the minutes of Stockfish analysis a game takes, but the threshold was set in advance and was missed.

### Study recommendations

For the 10 planted games of PilotA and PilotB the coach recommended a whole game to study every time, and **9 of 10**
picks contain the same weakness (threshold: 90%, so this just passes). For the sparse player it recommended none, as intended.
Known weakness: the picks lean on the newest games (game 24 was picked for most of PilotA's reviews), so the player
gets little variety.

### Threshold summary

| # | Result | Pass? |
| --- | --- | --- |
| 1 Evidence accuracy | 100% | pass |
| 2 Overclaims (graph) | 0 | pass (single specialist: 4, fails) |
| 3 Later games cited as earlier | 0 | pass, **after a fix** (it failed at first) |
| 4 Planted move selected | 8/8 | pass |
| 5 Planted move compared | 8/8 | pass |
| 6 False alarms on the steady player | 0 | pass |
| 7 Sparse player declines | 3/3 | pass |
| 8 Study pick shows the weakness | 9/10 | pass, narrowly |
| 9 Evidence speed-up | 4x (PilotA), 22x (PilotB) | pass |
| 10 Graph/fixed runtime ratio | about 70x | **fail** |

## 4. Representative outputs (deterministic mode, copied from a real run)

Order matters: the coach only compares a game with games played **before** it.


#### PilotA, first game: no earlier games
_Compared 2 key decisions: 2 judged within this game only, for lack of comparable games._
- **37. Ke3: judged within this game only** [narrowed, match: none]
  a4 advances the passed pawn (+4.8); after Ke3 it is +3.5. None of your other analyzed games had the same kind of decision in the same phase. So this is not compared with your usual play yet.
  cites: game 1 move 37.
- **9. Qa5: judged within this game only** [narrowed, match: none]
  This move cost 13 win-% points (Qg5 was stronger). None of your other analyzed games had the same kind of decision in the same phase. So this is not compared with your usual play yet.
  cites: game 1 move 9.
  study pick: none (the coach declined to pick a game)

#### PilotA, game 3: only two earlier games
_Compared 2 key decisions: 2 judged within this game only, for lack of comparable games._
- **4. Ke2: judged within this game only** [narrowed, match: theme]
  Ke2 moved the king before castling while queens are on the board. d3 kept the king safer. Only 2 other games had the same kind of decision in the same phase; 3 are needed to compare. So this is not compared with your usual play yet.
  cites: game 3 move 4.
- **32. Qf2: judged within this game only** [narrowed, match: theme]
  After Qf2 the opponent has mate in 17. Qxd2 avoided it (-8.2). Only 1 other game had the same kind of decision in the same phase; 3 are needed to compare. So this is not compared with your usual play yet.
  cites: game 3 move 32.
  study pick: none (the coach declined to pick a game)

#### PilotA, last game: 23 earlier games
_Compared 3 key decisions: 1 decision worse than your usual play, 2 in line with your usual pattern._
- **4. Ke2: your usual pattern in king safety in the opening** [similar, match: exact]
  In line with your usual play. Across 23 decisions in 23 earlier games with the exact same position you went wrong 39% of the time. Here the move cost 18 win-% points (d3 was stronger). Ke2 moved the king before castling while queens are on the board. d3 kept the king safer.
  cites: game 24 move 4., game 23 move 4., game 22 move 4., game 21 move 4.
- **19. Bg5: worse than usual in tactical moments** [worse, match: theme]
  Worse than usual. This move cost 15 win-% points (Rae1 was stronger), but across 35 decisions in 18 earlier games with the same kind of decision in the same phase you never went wrong. After Bg5, the line Qxe5 loses about 5 points of material. Rae1 avoided it.
  cites: game 24 move 19., game 23 move 14., game 23 move 20., game 22 move 14.
- **34. h3: your usual pattern in passed pawns in the endgame** [similar, match: theme]
  In line with your usual play. Across 12 decisions in 6 earlier games with the same kind of decision in the same phase you never went wrong. Here the move cost 5 win-% points (e6 was stronger). e6 advances the passed pawn (-2.7); after h3 it is -3.5.
  cites: game 24 move 34., game 20 move 44., game 20 move 45., game 19 move 34.
  study pick: game 23

#### PilotC (2 games), second game
_Compared 1 key decision: 1 judged within this game only, for lack of comparable games._
- **4. Ke2: judged within this game only** [narrowed, match: theme]
  Ke2 moved the king before castling while queens are on the board. d3 kept the king safer. Only 1 other game had the same kind of decision in the same phase; 3 are needed to compare. So this is not compared with your usual play yet.
  cites: game 50 move 4.
  study pick: none (the coach declined to pick a game)


More cases where the app declines to claim:

- A four-move game (`e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#`): the report says it is too short for an improvement verdict and shows only the single strongest observation (`test_four_move_game_gets_no_improvement_verdict_but_a_strongest_observation`).
- A game made only of forced moves: "no decisions", no findings, no one to blame for the loss (`test_game_with_only_forced_choices`).
- A game analyzed under an older analysis version is shown as not analyzed, with the reason, instead of silently mixing versions (`test_game_analyzed_under_an_older_version_is_flagged_for_reanalysis`).
- A job interrupted mid-analysis shows as "running" and never as finished or partly finished (`test_interrupted_run_is_visible_as_partial_and_never_as_complete`).

## 5. Bugs the evaluation found

1. **"Earlier games" included later games.** Reviewing the very first game of PilotA said it was compared with 9 "earlier games"; they were the games played afterwards.
   Fixed: history is now only games played before the one being reviewed (`earlier_pool` in `compare.py`; test `test_history_only_ever_cites_games_played_before_the_one_being_reviewed`).
2. **Two decisions from one game could count as a pattern.** The match was "strong" with two exact-position decisions even when they came from a single game, which contradicts the rule of 3 earlier games. Fixed: strength is counted in distinct games.
3. **An empty or non-PGN upload "completed" with no message.** It now says no games were found and what to check (`test_malformed_files_import_nothing_and_say_why`).

## 6. Known failure cases and limits

- **Not measured: model-backed mode.** The harness supports it (`python -m eval.run_eval evaluate --model` with a provider key in the environment) but no key was available in the environment where this ran, so the model's contribution is **not yet visible**. The router and the verifier are tested with fake providers (rate limits, invented moves, timeouts), which shows the fallbacks work but not how good the model's wording is.
- **No human participants.** "Can players identify what changed / find the cited positions" is measured as: does the output name the change, and how many games must be opened to find three examples. A human pilot should follow the same sample and thresholds.
- **Synthetic games.** One opening and one planted weakness give positions that repeat far more than real play does, so the 8/8 is an upper bound. Real histories will mostly produce "narrowed" and "emerging", which is the intended behaviour but is untested on real people.
- **21% of selected decisions cost under 5 win-% points.** Some picks are strengths or small positional notes rather than mistakes.
- **Coarse comparisons can sound stronger than they are.** "Worse than usual ... you never went wrong" at the "same kind of decision in the same phase" level compares loosely related positions. The text says so ("similar in kind, not identical"), but a reader could take it as more than it is.
- **Engine noise.** A verdict near the 5/10/20-point boundaries can flip with the engine's node budget; findings where the quick and deep passes disagree are labelled uncertain rather than hidden.
- **Study picks lean on recent games** (above).

## 7. Failure-case tests (all in `backend/tests/`, run on SQLite and on PostgreSQL)

| Area | Tests |
| --- | --- |
| Imports | duplicate uploads and re-uploads, ambiguous players, malformed/empty/binary/illegal PGNs, missing ratings, provider throttling, server errors, garbage and timeouts, concurrent identical uploads, unknown users |
| Jobs | concurrent workers (4 threads) claim each job once, concurrent enqueue makes one job, worker restart takes over an expired lease and finishes once, clean shutdown does not use an attempt, cancellation before and after analysis, bounded retries, interrupted runs never look complete |
| Versions | a game analyzed only under an older version is flagged, counted on the dashboard and shown with a reason in the report |
| Chess evidence | reviewed opening, middlegame and endgame positions: every stored move, best move and line is legal from its own position; side to move matches; mirrored games (colours swapped) give the same verdicts; transpositions share a position key and side to move, castling rights and en passant separate keys; an even trade and a sound sacrifice are not labelled as losing material; a game that ends in the opening gets no middlegame findings |
| Study workflow | answers stay hidden across refreshes and in listings, a model timeout falls back to standard text for reviews, studies and reveals, manual and suggested selections both lead to usable reviews, study picks come from wins, losses and draws and never from unanalyzed games |
| Database | migrations match the models, repeat safely, and the whole suite also passes on PostgreSQL 16 (`CHESS_TEST_PG=1`) |

## 8. Reproduce

```bash
cd backend
.venv/Scripts/python -m eval.gen_games                    # optional: regenerates eval/data (the committed files are what was used)
.venv/Scripts/python -m eval.run_eval analyze  --db eval/results/eval.db   # about 7 minutes with 3 engines
.venv/Scripts/python -m eval.run_eval evaluate --db eval/results/eval.db   # add --model with a provider key to measure the model
.venv/Scripts/python -m eval.examples --db eval/results/eval.db
```
