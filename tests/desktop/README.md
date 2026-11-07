# Desktop render harness

Loads `desktop/plugin.js` with a stubbed `@hermes/plugin-sdk`, registers it against a fake plugin context,
and server-renders every contribution with fixtures produced by `hypnos/webapi.py`.

```bash
cd tests/desktop && npm install
sed "s#'@hermes/plugin-sdk'#'./sdk-stub.mjs'#" ../../desktop/plugin.js > plugin.mjs
node run.mjs fixtures.json
```
