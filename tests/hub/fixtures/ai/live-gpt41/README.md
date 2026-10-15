# One live round with gpt-4.1 after the `api` check (2026-10-09)

Same prompt as the coordinator's draw_disc run (`prompt.txt`), no start-from, model
`gpt-4.1` through the Mana router (OpenAI chat/completions, json_schema strict,
temperature 0.2), kit at feature/ai-author-2 before the private-member wording and the
Phase listing were added. Three requests, about US$0.21
(55308 prompt tokens, 24960 cached; 17111 completion tokens).

- Plan: valid on the first answer (`plan.json`).
- Source, attempt 1: refused by `api` (a hand-written `HoldWithFlash(phases.HoldFixation)`
  reading HoldFixation's state as `self.on_break`, `self.duration_s`, `self._entered`,
  `self.then`) and by `documentation` (an unknown `color` field in the diagram).
- Source, attempt 2 (the repair): the documentation was fixed; `api` still refused it:
  `HoldFixation(duration=...)` (the parameter is `duration_s`) and the same subclass reading
  `self.on_break`, `self.entered`, `self.duration`, `self.on_advance`, none of which exist.
  Every other check passed (`source-report.json`).

**The generated package does not pass the checks.** It is kept as the record of this
round: each refused line would have raised AttributeError or TypeError at run time, and
before the `api` check it would have been accepted like the draw_disc package. The model
was not run again (budget). After this round the kit names the private state the subclass
was reaching for (`HoldFixation keeps it private as _on_break ...`), lists the `Phase`
protocol, and the source prompt says to compose provided phases (a stimulus during part of
a hold = three HoldFixation phases) or write an own Phase; that has not been tried live.
