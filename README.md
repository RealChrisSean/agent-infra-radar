# Agent Infra Radar

A job-search demo built with [Sprites](https://sprites.dev/) and [Resend](https://resend.com/). It checks official company job feeds, keeps a searchable dashboard, and emails the top 10 new matches with a link to the rest.

I used **Claude CoWork and ChatGPT Work** to run the schedulers for this demo. My current setup searches **daily at 4 PM Pacific (`America/Los_Angeles`)**. After the search finishes, it sends an email with new matching roles once the Resend connector is added to the Sprite dashboard, enabled for that Sprite, and a sender and recipient are configured.

The repository contains the app and the command those schedulers run. Cloning it does not create a Claude CoWork, ChatGPT Work, or cron schedule for you.

## What it does

- Reads official Ashby, Greenhouse, and Lever job feeds from the employers configured in Settings.
- Defaults to agent infrastructure software engineering roles with published annual USD base salary minimums of at least **$150,000**, located in **San Francisco** or eligible for **US remote** work.
- Lets you change salary, locations, keywords, sources, your experience summary, and email settings in the app.
- Keeps saved, applied, and hidden states across searches.
- Shows completed and failed sources separately, so a partial search is visible.
- Sends one digest containing up to **10 best new matches**, with reasons, salary, location, and application links. If more qualify, a link opens the full list in your dashboard.

Ranking uses shared technical terms from your optional experience summary, direct agent infrastructure relevance, and your search keywords. It is a transparent relevance ranking, not an assessment that you meet every qualification. Missing or ambiguous salary, location, or role evidence goes to **Needs review** and is not emailed. A job disappearing from a feed does not automatically mean it has closed.

## Run locally

Use **Python 3.11 or newer** on macOS or Linux. The app uses the Python standard library and SQLite; no Python packages are required.

```sh
python3 server.py
```

Open [http://localhost:8080](http://localhost:8080). Start a search from the dashboard, or run one without email delivery:

```sh
python3 infra_runner.py --no-notify
```

Settings and results are created in `data/`, which is excluded from Git. The server binds to localhost by default. To choose a different data directory:

```sh
RADAR_DATA=/tmp/agent-infra-radar HOST=127.0.0.1 PORT=8080 python3 server.py
```

Local search and preview work without Resend. Connector discovery and delivery require a Sprite with access to a Resend connector.

## Run on a Sprite

1. Create a Sprite and copy or clone this repository into it.
2. Run `HOST=0.0.0.0 python3 server.py` as a persistent Sprite service on port **8080**.
3. Keep the Sprite URL private and authenticated. The app relies on the Sprite proxy for access control and contains personal search settings and job-tracking data.
4. Set your dashboard URL as described below so digest links open your installation.
5. Open the app, review **Search settings**, and run your first search.

The runtime is a small Python server with a SQLite database on the Sprite filesystem. It does not require a separate database service or an LLM API key.

## Connect Resend

1. In your **Sprite dashboard**, add the Resend connector and allow it for the Sprite running this app.
2. In Resend, verify a sending domain that you control.
3. In the app's **Search settings**, enter your recipient and a sender on the verified domain, such as `Radar <jobs@yourdomain.com>`.
4. Click **Check connection**. The app discovers the Resend gateway automatically. If multiple connections exist, choose the intended connector's gateway URL in Settings.
5. Use **Preview digest** to inspect the email, then **Email top 10 matches** to send it.

The app calls the Sprites connector gateway; it does not ask you to paste a Resend API key into the app or store one in this repository. The default Resend testing sender has recipient restrictions, so use your verified domain for delivery to other addresses.

After a manual or scheduled search, email delivery runs if it is enabled and configured. A digest is sent only when new eligible matches are queued; an unchanged list does not generate a daily duplicate. Successful digests also mark the additional matches covered by the dashboard link as notified. Delivery state and idempotency keys are persisted to reduce duplicate sends on retries. Applications remain manual.

## Schedule the daily search

The demo's schedule is **4 PM Pacific every day**. Configure your scheduler in the `America/Los_Angeles` timezone so daylight saving changes are handled correctly.

Give Claude CoWork, ChatGPT Work, or another scheduler access to your Sprite and have it run this command from the repository directory at that time:

```sh
python3 infra_runner.py --scheduled
```

A useful scheduler instruction is:

> Every day at 4 PM America/Los_Angeles, connect to my job-search Sprite and run `python3 infra_runner.py --scheduled` from the app directory. Report a failure or any required action; stay quiet when there is nothing new. The runner handles the search and Resend email delivery.

Use one active schedule for an installation. The runner locks concurrent searches and skips a repeated scheduled run after a successful or partial run on the same Pacific date. The app's **Enable the daily search** toggle lets you pause scheduled work, but does not create or delete an external schedule.

After you have connected your scheduler, create `data/agent-infra/schedule-active.json` containing `{}` to make the dashboard show the schedule as connected. This file is a status marker, not a scheduler.

## Dashboard links

Set `RADAR_PUBLIC_URL` to your app's base URL in both the web service and the scheduler's environment. For example, replace the placeholder below with your own authenticated Sprite URL:

```sh
RADAR_PUBLIC_URL=https://radar.example.com HOST=0.0.0.0 python3 server.py
RADAR_PUBLIC_URL=https://radar.example.com python3 infra_runner.py --scheduled
```

The digest adds `/#jobs` to this URL. It defaults to `http://localhost:8080` for local development; set it before sending emails from a Sprite. The URL contains no account credentials.

## Development

The core files are:

| File | Purpose |
| --- | --- |
| `infra_scan.py` | Fetch and classify job listings |
| `infra_sources.json` | Default employer feeds |
| `infra_store.py` | Settings, tracking, search history, and delivery state |
| `infra_notify.py` | Ranking, email preview, connector discovery, and delivery |
| `infra_runner.py` | Manual and scheduled search entry point |
| `server.py` | HTTP API and static UI |
| `public/infra.*` | Dashboard and settings UI |

Run the Python checks:

```sh
python3 -m unittest discover -p 'test_*.py'
```

The optional browser test uses Node.js, Playwright, and Chrome:

```sh
npm install --no-save --package-lock=false playwright
node test_infra_ui.mjs
```

Tests use temporary state and synthetic fixtures. They do not send live emails.

## Data and privacy

This repository ships source code, tests, and public source configuration. Personal settings, profiles, job history, email addresses, connector routing details, delivery records, and scheduler state belong in the ignored `data/` directory. Do not commit that directory, databases, `.env` files, logs, or runtime exports.

Deploying source does not require copying another installation's runtime data. Keep your app authenticated before entering personal information or enabling email delivery.

## License

[MIT](LICENSE).
