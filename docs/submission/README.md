# Submission checklist

Everything here is for you to run; nothing has been pushed, posted or submitted.

## 1. Push the repository

```bash
cd ~/hackathon/hypnos
gh repo create 0xNoramiya/hypnos --public --source . --remote origin --description "Hermes learns in its sleep"
git push -u origin main
```

If you use another owner or name, update `homepage` in `plugin.yaml`, the install line in `README.md` and
`docs/runpod.md`, and `repo` in `docs/submission/hypnos.yaml`.

## 2. Check the pinned commit

```bash
git rev-parse HEAD                       # the 40-character SHA to pin
hermes plugins validate . --install-deps # must print "Validation passed."
hermes plugins doctor . --ci
```

## 3. Screenshots

Capture the five shots listed in `docs/submission/PR.md` from Hermes Desktop or the Webapp on the demo
profile, save them in `docs/screenshots/`, commit and push. Use their raw URLs at the pinned SHA in the
catalog entry, for example
`https://raw.githubusercontent.com/0xNoramiya/hypnos/<SHA>/docs/screenshots/report.png`.

## 4. Catalog pull request

1. Fork `NousResearch/hermes-agent` and add one file, `plugin-catalog/hypnos.yaml`, from
   `docs/submission/hypnos.yaml` with `sha` set to the commit from step 2 and the screenshot URLs filled in.
2. Open the PR with `docs/submission/PR.md` as the description.
3. Wait for the catalog CI job; it runs `hermes plugins validate` at the pinned commit.

Any later fix to the plugin means a new commit and a new PR that bumps `sha` and `version`.

## 5. Video and form

- Record with `docs/demo-script.md` (under 90 seconds, "synthetic demo data" caption on screen).
- Post on X tagging @NousResearch, with the repository link.
- Submit https://form.typeform.com/to/FwJLvOln.

Deadline: 2026-11-11 17:00 EST (2026-11-12 06:00 WITA).
