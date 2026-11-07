# Screenshots

`preview-*.png` are previews: the real `desktop/plugin.js` components, fed by the real REST data of the
five-day GPU timelapse in the README (Qwen3-0.6B on an A40, synthetic demo data), rendered with Hermes
Desktop's own theme token values by `tests/desktop/preview.mjs`. The window frame around them is a mock-up,
not the Hermes app.

- `preview-report-dark.png`, `preview-report-light.png`: the `/hypnos` page on night 2, the last promoted
  night. The fixture points the page at that night, so the picker still reads "Latest night".
- `preview-reply-badge.png`: the real day-5 turn behind the badge ("Push the csv export work.", answered by
  brain v2 in two calls).

To rebuild them from any Hypnos database:

```bash
python tests/desktop/make_fixtures.py DATA.db tests/desktop/fixtures.json
cd tests/desktop && npm install
sed "s#'@hermes/plugin-sdk'#'./sdk-stub.mjs'#" ../../desktop/plugin.js > plugin.mjs
node preview.mjs fixtures.json OUT_DIR    # then screenshot OUT_DIR/*.html at 1230 px wide
```

Captures from Hermes Desktop itself belong here too; the checklist is in `docs/submission/PR.md`.
