# gpt-4.1's draw_disc package (live failure, 2026-10-09)

The coordinator's integrated run (preview hub, gpt-4.1 through the Mana router) produced a
package that passed all ten static checks of that time, was accepted, installed on the rig
and failed in `--mode simulate --headless` on trial 2:
`AttributeError: 'SimulatedDisplay' object has no attribute 'draw_disc'` (its flash
stimulus called `self.display.draw_disc(...)`; alhazen's display has no drawing methods).

- `prompt.txt`, `plan.json`: the draft's prompt and accepted plan (from the hub's draft
  record `44e7be5e569a87c719d076ae23b09a28`).
- `source-answer.json`: the source answer, rebuilt from the installed package
  (`fixation-flash 0.1.0`, sha256 170226a4…); the kit assembles from it a task module
  byte-identical to the one the rig ran. The provider's raw text was not stored by the hub.
- `repaired-answer.json`: the same answer with both hand-written `FlashStimulus` classes
  replaced by `make_fixation(..., pos=...)` (what the repair round is expected to do).

The `api` check now refuses the first and accepts the second (tests in test_ai_author.py §6).
