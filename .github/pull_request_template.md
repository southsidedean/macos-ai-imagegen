<!-- What this changes and why. Link the issue if there is one. -->

- [ ] `scripts/check.sh` passes
- [ ] Tests come with the change (a bug fix starts with a test that fails without it)
- [ ] A change to the page: `MIA_BROWSERS=chrome,firefox uv run pytest tests/test_browser.py` passed (CI adds Safari)
- [ ] An engine change: `scripts/real-engine.sh` passed on a Mac (CI never runs a real model)
- [ ] Docs and `PLAN.md` updated in this pull request, if it changes a decision
- [ ] Text people read was reviewed for AI writing patterns (the `humanizer` skill)
- [ ] Every new or changed image was looked at, and committed with `IMAGES_CHECKED=1`
- [ ] `CHANGELOG.md` has a line under the next version if users will notice the change
- [ ] This description passes `scripts/private-names.sh check` (run on a copy of it before posting)
