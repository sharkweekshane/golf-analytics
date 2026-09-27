# golf-caddie Worker

The relay behind the Caddie page on GitHub Pages: it holds the Meta Model API key (a Cloudflare secret),
checks the page's passcode and forwards one Responses API call at a time. Setup, architecture, cost and
abuse notes are in the project README, section "Caddie on GitHub Pages".

```sh
npm install
npm test                       # node --test (Node 22); Meta's API is stubbed
npx wrangler deploy --dry-run  # checks wrangler.toml without deploying
```

- `src/index.js`: the entry point (default export only; workerd treats named exports as entrypoints).
- `src/relay.js`: everything the Worker enforces, described at the top of the file.
- `wrangler.toml`: the name, `[vars]` (allowed origin, model, effort, output limit, Meta base URL) and the
  two rate limits. The secrets `MUSE_API_KEY` and `CADDIE_PASSCODE` are set with `npx wrangler secret put`.
