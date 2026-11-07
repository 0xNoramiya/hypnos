# Submission checklist

Everything here is for you to run; nothing has been pushed, posted or submitted.

## 1. Push the repository

```bash
cd ~/hackathon/hypnos
gh repo create 0xNoramiya/hypnos --public --source . --remote origin --description "Hermes learns in its sleep"
git push -u origin main
```

If you use another owner or name, update `homepage` in `plugin.yaml`, the install line in `README.md` and
`docs/runpod.md`, and `repo` plus the image URLs in `docs/submission/hypnos.yaml` and `PR.md`.

## 2. Check the commit you will pin

```bash
git rev-parse HEAD                       # the 40-character SHA to pin
hermes plugins validate . --install-deps # must print "Validation passed."
hermes plugins doctor . --ci
```

The banner (`docs/banner.png`) and two screenshots (`docs/screenshots/preview-*.png`) are already in the repo
and referenced by the entry. Captures from Hermes Desktop itself are optional; if you add some, commit them
before you take the SHA and add their URLs to the entry and `PR.md`.

## 3. Post the trailer

- The trailer is `../hypnos-trailer/renders/hypnos-trailer.mp4` (88 s, 1080p). The script is
  `docs/demo-script.md`.
- Post it on X tagging @NousResearch, with the repository link.
- Posted: https://x.com/acetaminofenol/status/2119120790495039622 (already in `PR.md` and the README).

## 4. Catalog pull request

Opened: https://github.com/NousResearch/hermes-agent/pull/150800, pinned to
`0a809b9085fdeaed63fd909b927afcc41de512de`, branch `feat/plugin-catalog-hypnos` on the fork. The steps below
are how it was made, for the next SHA bump.

```bash
SHA=$(git -C ~/hackathon/hypnos rev-parse HEAD)
cd ~/hackathon
gh repo fork NousResearch/hermes-agent --clone -- --depth 1
cd hermes-agent
git checkout -b feat/plugin-catalog-hypnos
grep -v '^#' ~/hackathon/hypnos/docs/submission/hypnos.yaml | sed "s/<SHA>/$SHA/g" > plugin-catalog/hypnos.yaml
git add plugin-catalog/hypnos.yaml
git commit -m "feat(plugin-catalog): add hypnos plugin"
git push -u origin feat/plugin-catalog-hypnos
sed "s/<SHA>/$SHA/g" ~/hackathon/hypnos/docs/submission/PR.md > /tmp/hypnos-pr.md
gh pr create --repo NousResearch/hermes-agent --title "feat(plugin-catalog): add hypnos plugin" --body-file /tmp/hypnos-pr.md
```

Before creating the PR, read `/tmp/hypnos-pr.md` once: tick the checklist items that are yours to tick
(contributing guide, duplicate search, commit message) and check that the trailer link is in. Then wait for
the catalog CI job; it clones the repo at the pinned commit and runs `hermes plugins validate`.

Any later fix to the plugin means a new commit and a new PR that bumps `sha` and `version`.

## 5. Form

Submit https://form.typeform.com/to/FwJLvOln with the repository, the X post and the catalog PR.

Deadline: 2026-11-11 17:00 EST (2026-11-12 06:00 WITA).
