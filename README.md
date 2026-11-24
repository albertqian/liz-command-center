# Phoenix Career Radar

GitHub Actions agent. Mon and Thu, 10am Phoenix: industry trends, Phoenix-area events, career strategy, emailed in a pink and blue layout.

## Setup
1. GitHub: New repository, private, name `phoenix-career-agent`. Upload every file in this folder (keep `.github/workflows/career-radar.yml` path).
2. Repo: Settings > Secrets and variables > Actions > New repository secret. Add four:
   - `ANTHROPIC_API_KEY`
   - `GMAIL_USER` (sender Gmail address)
   - `GMAIL_APP_PASSWORD` (Google Account > Security > 2-Step Verification > App passwords)
   - `TO_EMAIL` (recipient; comma-separate for several). Test with your own address first.
3. Repo: Settings > Actions > General > Workflow permissions > Read and write permissions > Save.
4. Actions tab > Phoenix Career Radar > Run workflow. Check the inbox.

## Cost
About $0.05-0.15 per run (Haiku plus 3 web searches at $10 per 1,000). Roughly $1/month. Lower `MAX_SEARCHES` in the workflow env to cut more. GitHub Actions minutes: free tier covers it.

## Notes
- Cron is 9:30am Phoenix; the script holds the send until 10:00. Manual runs send immediately.
- `seen.json` tracks sent events and trends to avoid repeats.
- Events are dropped unless they have a real URL and a future date. Always verify on the organizer's page.
- RSS feed list is in `agent.py` (`FEEDS`). A dead feed is skipped, not fatal.
- Update `profile.md` when her situation changes.
