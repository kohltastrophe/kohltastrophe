# Sponsors SVG Workflow

Generates a unified, light/dark-aware `sponsors.svg` wall and platform CTA cards (`sponsor-github.svg`, `sponsor-kofi.svg`) from GitHub Sponsors and Ko-fi. Generated SVGs and automated Ko-fi state are published to an orphan `badges` branch so `main` stays clean.

- **Tiered layout:** Recurring sponsors at `$25+/mo` appear in the **Featured sponsors** tier (64px avatars); lower recurring tiers and one-time supporters appear in the **Sponsors** tier (46px avatars).
- **Cross-platform deduplication:** Supporters who sponsor on both GitHub and Ko-fi are merged by profile identity, summing active recurring monthly amounts across platforms.
- **Avatar resolution:** Ko-fi supporters can tag `@username`, `gh:username`, `github.com/username`, `roblox:username`, or `roblox.com/users/<id>` in their name or message to link their profile and avatar automatically. Other supporters fall back to Gravatar (via SHA-256 email hash) or initials.
- **Zero bot commits on `main`:** Automated Ko-fi webhook state lives in `sponsors.json` on the single-commit orphan `badges` branch alongside the rendered SVGs, with a 35-day rolling active window.

---

## Using This on Your Own Profile

### 1. Configure `.github/FUNDING.yml` and `README.md`

Set your GitHub and Ko-fi handles in `.github/FUNDING.yml`. `scripts/sponsors.py` reads this file automatically:

```yaml
github: your-github-username
ko_fi: your-kofi-username
```

Embed the generated SVGs from your `badges` branch in `README.md`:

```html
<p align="center">
  <a href="https://github.com/sponsors/your-github-username"><img src="https://raw.githubusercontent.com/your-github-username/your-github-username/badges/sponsor-github.svg" alt="GitHub Sponsors" /></a>&nbsp;&nbsp;<a href="https://ko-fi.com/your-kofi-username"><img src="https://raw.githubusercontent.com/your-github-username/your-github-username/badges/sponsor-kofi.svg" alt="Ko-fi" /></a>
  <a href="https://github.com/sponsors/your-github-username"><img src="https://raw.githubusercontent.com/your-github-username/your-github-username/badges/sponsors.svg" alt="Sponsors" /></a>
</p>
```

### 2. Add `SPONSORS_TOKEN` Repository Secret

GitHub's GraphQL API returns `tier: null` on `sponsorshipsAsMaintainer` when queried with the default `GITHUB_TOKEN`. To read your sponsor tier pricing:

1. Create a **Personal access token (classic)** at [github.com/settings/tokens](https://github.com/settings/tokens) with the `read:user` scope.
2. Add it as a repository secret named `SPONSORS_TOKEN` under **Settings -> Secrets and variables -> Actions** (or run `gh secret set SPONSORS_TOKEN`).

The workflow in `.github/workflows/sponsors.yml` runs every 6 hours and on `workflow_dispatch`.

### 3. Deploy the Webhook Relay Worker (Optional)

To update your badges immediately when someone sponsors you on Ko-fi or GitHub Sponsors, deploy `scripts/kofi-webhook-worker.js` to Cloudflare Workers.

1. Set your repository name in `wrangler.toml`:

```toml
name = "sponsors-webhook"
main = "scripts/kofi-webhook-worker.js"
compatibility_date = "2026-11-01"
workers_dev = true

[vars]
GITHUB_REPO = "your-github-username/your-github-username"
```

2. Deploy the Worker and configure its secrets:

```bash
npx wrangler deploy
npx wrangler secret put GITHUB_DISPATCH_TOKEN
npx wrangler secret put KOFI_VERIFICATION_TOKEN
npx wrangler secret put GITHUB_WEBHOOK_SECRET
```

- `GITHUB_DISPATCH_TOKEN`: Fine-grained GitHub PAT scoped to your repository with **Contents: Read and write** permission (required to trigger `repository_dispatch`).
- `KOFI_VERIFICATION_TOKEN`: Verification token shown on [ko-fi.com/manage/webhooks](https://ko-fi.com/manage/webhooks).
- `GITHUB_WEBHOOK_SECRET`: Random secret string (for example, `openssl rand -hex 32`) configured on your GitHub Sponsors webhook at `https://github.com/sponsors/<your-username>/dashboard/webhooks` (Payload URL: your Worker URL, Content type: `application/json`).

### 4. Configuration, Manual Overrides, and Local Preview

You can customize tier thresholds, rolling active windows, custom Ko-fi tier names, allowed profile hosts, and manual sponsor entries in `.github/sponsors.json` on `main`:

```json
{
  "featured_threshold": 25,
  "active_window_days": 35,
  "kofi_tiers": {
    "supporter": 1,
    "backer": 5,
    "featured": 25
  },
  "allowed_hosts": ["github.com", "roblox.com", "ko-fi.com"],
  "sponsors": [
    {
      "platform": "kofi",
      "login": "octocat",
      "name": "The Octocat",
      "monthly_dollars": 25,
      "is_one_time": false,
      "url": "https://github.com/octocat",
      "expires_at": "2026-12-31T00:00:00Z"
    }
  ]
}
```

All settings can also be overridden via CLI flags (`--login`, `--kofi`, `--featured-threshold`, `--active-window-days`, `--extra`) or environment variables (`GITHUB_LOGIN`, `KOFI_HANDLE`, `SPONSORS_FEATURED_THRESHOLD`, `SPONSORS_ACTIVE_WINDOW_DAYS`, `SPONSORS_KOFI_TIERS`, `SPONSORS_ALLOWED_HOSTS`, `EXTRA_SPONSORS_JSON`).

Run the test suite and preview the demo wall locally:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/sponsors.py --demo --output /tmp/sponsors.svg
```
