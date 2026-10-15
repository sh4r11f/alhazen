A trial is two phases on one stimulus. **Acquire** waits for gaze to enter the window, for
at most [[param:acquire_timeout]]; how long it lasts depends on the subject. **Hold** then
lasts [[param:hold_duration]] if gaze stays inside the [[param:fix_window_dva]] window.

The hold is checked on every frame before it is drawn, so a break on the frame the hold would
have finished on is still a break. The trial's last flip takes the point off; the runner then
waits [[param:iti]] before the next trial.

To change what a trial is, edit `build_trial` in `src/fixation_demo/task.py`, then update this
page and `docs/experiment.json` with it.
