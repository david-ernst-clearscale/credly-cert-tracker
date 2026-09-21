# Building a Serverless Certification-Compliance Dashboard on AWS

*How we track AWS and Anthropic certifications across a consulting team, reconcile them against the AWS Partner Network, and keep partner-tier compliance visible — entirely serverless, defined in code.*

---

## The problem

As an AWS (and now Anthropic) partner, a consultancy's partner tier is gated on **how many people hold which certifications**. AWS Partner Network (APN) tiers require specific counts across categories — e.g. a Premier-tier target of *10 Foundational, 25 Technical, 10 Professional/Specialty* active certifications — and AWS gives **no grace period** when a cert lapses. The Claude Partner Network adds a parallel requirement (e.g. 10 Architect–Foundations certs).

The raw data lives in two places that don't talk to each other:

1. **Credly** — where individuals' badges actually live (AWS and Anthropic both issue through Credly). Each person has a public badge page.
2. **The AWS APN portal** — AWS's own view of which of your people hold which certs, exported as a CSV.

Neither is a dashboard, neither reconciles against the other, and nobody wants to eyeball two spreadsheets before a partner review. This app closes that gap: it **syncs badges from Credly**, **computes partner-tier progress**, **surfaces upcoming expirations**, and **reconciles the Credly-derived picture against the APN export** so you can see exactly who's counted, who's missing, and who's about to fall off.

---

## Architecture at a glance

Everything is serverless and defined with the **AWS CDK (Python)**. One `cdk deploy` stands up the whole system.

```
                         ┌──────────────────────────────┐
   Google login          │  CloudFront + S3 (SPA)        │
   (Cognito hosted UI) ──▶│  React + Vite dashboard       │
                         └───────────────┬───────────────┘
                                         │ Bearer id_token
                                         ▼
                         ┌──────────────────────────────┐
                         │  API Gateway (REST)           │
                         │  Cognito authorizer           │
                         │   GET  /compliance            │
                         │   POST /apn-roster            │
                         └───────────────┬───────────────┘
                                         ▼
                         ┌──────────────────────────────┐
                         │  dashboard_api Lambda         │
                         └──────┬─────────────┬──────────┘
                    reads certs │             │ reads/writes roster
                                ▼             ▼
             ┌───────────────────────┐   ┌──────────────┐
             │ DynamoDB              │   │ S3 (private) │
             │  credly-users         │   │  apn_roster  │
             │  credly-certifications│   └──────────────┘
             └──────────▲────────────┘
                        │ upserts
   EventBridge (7AM) ──▶│  badge_sync ──▶ Credly public badges.json
   EventBridge (7AM) ──▶│  expiration_checker (recompute status)
                        │  compliance_reporter ──▶ CloudWatch metrics ──▶ Alarms ──▶ SNS
                        │  notification_handler ──▶ SES email / Slack
```

**Stack building blocks:**

| Concern | Service |
|---|---|
| Frontend hosting | S3 (private) + CloudFront with Origin Access Control |
| Auth | Cognito User Pool federated to Google, OAuth authorization-code flow |
| API | API Gateway REST + Cognito User Pools authorizer |
| Compute | 5 Python 3.12 Lambdas |
| Data | DynamoDB (2 tables, 1 GSI, streams enabled) |
| Roster storage | S3 (private, encrypted) |
| Scheduling | EventBridge Rules (cron) |
| Observability | CloudWatch metrics + alarms, SNS |
| Notifications | SES (email) + Slack webhook |
| Secrets | AWS Secrets Manager (Google OAuth client secret) |

---

## Data model

Two DynamoDB tables, both `PAY_PER_REQUEST`, both `RETAIN` on delete so data survives a stack teardown.

**`credly-users`** — partition key `employee_id` (e.g. `dana.whitfield`). Attributes: `credly_username`, `consent_status`, `email`, `name`, and an optional `role` (used to find admins for breach alerts). This is the roster of people we track; a user is only synced if `consent_status = opted_in`.

**`credly-certifications`** — partition key `employee_id`, sort key `certification_id`. Written by the sync job: `certification_name`, `credly_username`, `issued_at`, `expires_at`, `status`, `partner_tier_category`, `badge_url`, `last_synced`. A **global secondary index `by-expiration`** (partition `status`, sort `expires_at`) supports expiry-oriented queries, and the table has **DynamoDB Streams** enabled (`NEW_AND_OLD_IMAGES`) for future event-driven consumers.

---

## The data pipelines

### 1. Badge Sync — pulling from Credly

`badge_sync` runs daily and is the only component that talks to the outside world. Credly exposes each user's badges at a **public, unauthenticated** JSON endpoint:

```
https://www.credly.com/users/<username>/badges.json?page=<n>&page_size=48
```

For every opted-in user it paginates through that endpoint (`urllib`, 30-second per-request timeout), keeps only the badges whose template name contains **"AWS Certified"** or **"Claude Certified"**, and upserts each one into `credly-certifications`. On write it computes two derived fields:

- **Status** from the expiry date — `active`, then `upcoming_renewal` (≤90 days out), `expiring_soon` (≤60), `critical` (≤30), `expired` (past due).
- **Partner-tier category** from the cert name — Foundational / Technical / Professional-Specialty.

Because Credly needs no API key, the whole sync is just HTTP + DynamoDB. The tradeoff is that it's **sequential and I/O-bound**: runtime scales with (users × pages), which is worth remembering when the team grows — a long sync can outlast a synchronous client's socket timeout, so the job is best invoked asynchronously and given a generous function timeout.

### 2. Expiration Checker — rolling status forward

Certification status is a pure function of *stored expiry date* and *today's date* — it changes only when a cert crosses a 90/60/30/0-day boundary, which happens at most once per calendar day. So `expiration_checker` runs once daily, scans the certs table, recomputes each status, and writes back only the ones that changed (stamping `last_status_change`).

Crucially, this job depends on **nothing but the data already in DynamoDB and the clock** — not on whether today's Credly sync succeeded. That independence is deliberate (more on scheduling below).

### 3. Compliance Reporter — metrics and breach detection

`compliance_reporter` scans active certs, tallies counts and unique holders per tier, and computes a **projected** compliance percentage that subtracts certs already flagged as expiring. It publishes a family of CloudWatch metrics under the `CredlyCertTracker` namespace (`CurrentCerts`, `CompliancePercentage`, `ExpiringWithin90Days`, `ProjectedCompliance`, …) dimensioned by tier, and classifies each tier GREEN (≥100%) / YELLOW (≥80%) / RED (<80%). On a RED tier it fires an async `Event` invoke to the notification handler.

### 4. Notification Handler — email + Slack

`notification_handler` is a fan-in target for two event shapes:

- **`expiry_reminder`** — a per-person nudge ("your cert expires in N days"), delivered by **SES email** and an optional **Slack webhook** with severity emoji.
- **`compliance_breach`** — a leadership alert that scans the users table for `role contains admin` and emails each of them, plus a Slack blast.

It's subscribed to an SNS topic and also directly invokable, so multiple producers can reach it.

---

## The API layer

A single **`dashboard_api`** Lambda backs a REST API behind a **Cognito authorizer**, with two routes.

### `GET /compliance` — the dashboard's data

One handler call scans the certs table once and derives everything the UI needs:

- **Tier progress** for AWS (Foundational / Technical / Professional-Specialty) and Anthropic (CCAR-F / CCAR-P / CCDV-F / CCAO-F), each as `current / required / percentage`.
- **A leaderboard** using **dense ranking** — ties share a rank, and the cutoff is the top *10 distinct rank groups*, not the top 10 individuals, so a big tie for #1 doesn't crowd everyone else out.
- **De-duplication of re-issued badges.** A person can hold two Credly badges for the *same* credential (a recert issues a new badge id, and Credly sometimes returns one record with no expiry and one with a real one). The API collapses to a single entry per `(person, certification name)`, preferring the record with a real expiry over a `no-expiry` placeholder and the latest expiry among real ones — so tier totals and the leaderboard never double-count.
- **The APN cross-reference** (below).

Classification is recomputed *from the cert name* at read time (Foundational = Cloud/AI Practitioner; Professional-Specialty = anything "Professional" or "Specialty"; else Technical), which keeps the dashboard's grouping authoritative regardless of what was stored at sync time.

### `POST /apn-roster` — uploading the APN export

The APN CSV changes often, so rather than bake it into a deploy, the dashboard lets an authenticated user **upload the CSV directly on the tab**. The browser reads the file as text and POSTs the raw CSV; the Lambda parses it **server-side in Python** (one parser, no duplicated logic), strips a UTF-8 BOM if present, validates the expected columns, and stores the parsed roster as JSON in a **private, encrypted S3 bucket**. The next `GET /compliance` reads that uploaded copy; a **bundled roster is committed as a seed/fallback** so the tab works before the first upload and never 500s if S3 is briefly unreachable.

---

## The reconciliation engine (the interesting part)

The "AWS APN Network" tab answers a deceptively hard question: **does the person AWS lists in the APN portal line up with the badge we see on Credly?** The two systems don't share a key. The APN export lists *Dana Whitfield / dana@example.com*; Credly stores *employee_id `dana.whitfield`, username `dana-whitfield`*. Email-only matching misses her.

So matching is done on a **set of normalized identity keys**. For each person on each side we generate keys from name, `employee_id`, `credly_username`, and the email local-part, each in dotted and spaced variants (`dana whitfield`, `dana.whitfield`, …). Two records match if **any** key overlaps. This resolves the case above via the name while still cleanly separating unrelated people (validated to produce no false positives on the real roster).

From that matching the tab derives four buckets:

- **On Credly & APN** — appears on the APN export *and* has a Credly account. ✅
- **In App, Not on APN** — a Credly user who **holds an AWS cert** but isn't on the APN export. Scoped precisely: Anthropic-only holders and people with no AWS cert are excluded, using a set of `employee_id`s that have at least one `"AWS Certified"` badge. This is the actionable "AWS isn't crediting us for this person" list.
- **Redacted / Not Trackable** — the APN export withholds some identities (`XXXXXXX`). Those records keep their *cert* info even though the *person* is masked, so the tab reports them as a subtotal **grouped into the same three tiers** — visibility into what APN is reporting even when we can't attribute it to a name. Headcount is intentionally reported as *records*, not people, since the same person could hold several.

Because all of this keys off the active roster (uploaded or bundled), the whole reconciliation **re-derives automatically the moment a new CSV is uploaded** — no redeploy.

---

## The frontend

A **React + TypeScript + Vite** single-page app, built to a static bundle and served from a **private S3 bucket via CloudFront** (Origin Access Control; the bucket blocks all public access). A CloudFront error-response rule rewrites 404s to `/index.html` so client-side routing works.

**Auth** is a from-scratch OAuth authorization-code flow against Cognito's hosted UI:

- "Sign in with Google" redirects to the Cognito hosted UI federated to Google, with a random **CSRF `state`** value stashed in `sessionStorage`.
- On return, the app validates `state`, exchanges the code for tokens at Cognito's `/oauth2/token` endpoint (a **public client**, no secret in the browser), and stores the `id_token` / `refresh_token` / expiry in `localStorage`.
- The token **silently refreshes ~60s before expiry**; if the refresh token is dead, the user is signed out cleanly rather than hitting a raw 401.

Every API call sends the `id_token` as a Bearer token; a 401/403 triggers the same graceful sign-out. The dashboard renders four tabs — AWS, Anthropic, Leaderboard, and AWS APN Network — the last of which hosts the CSV upload control and the reconciliation tables, refetching `/compliance` in place after a successful upload.

---

## Scheduling design

Two recurring jobs run on **EventBridge Rules** (cron) at **07:00 UTC**: badge sync and the expiration check. They're kept as two separate functions on the same trigger because they have very different risk profiles — the sync is slow, external, and failure-prone; the checker is fast, internal, and compliance-critical. They don't need ordering: the checker derives status purely from stored expiry + date, and the sync sets status on anything it writes, so it doesn't matter which finishes first on a given morning.

There is also an **EventBridge *Scheduler*** path (distinct from Rules) that was designed to create per-cert, one-time reminders at 90/60/30 days before each expiry. It's currently **inert** (no scheduler role configured) and is redundant with the daily checker, which already detects the same threshold crossings — a good candidate for removal.

---

## Security posture

- **No self-signup.** The Cognito pool disables sign-up; the only way in is Google federation.
- **Secret never in the template.** The Google OAuth **client secret lives in Secrets Manager** and is referenced dynamically at synth time, so it never lands in the CloudFormation template in clear text. (The non-secret *client ID* is supplied via environment at deploy time — an empty value is the one thing that will make the IdP resource fail, so the deploy script loads it from `.env` and aborts early if it's missing.)
- **Private buckets end to end.** Both the SPA bucket and the roster bucket block all public access; CloudFront reaches the SPA only through Origin Access Control.
- **Locked CORS.** The API's CORS allow-origin is pinned to the real CloudFront domain (derived at synth time), and even API Gateway's own 4xx/5xx responses carry CORS headers so an expired token surfaces as a real 401 rather than a browser CORS error.
- **CSRF-protected login** via the OAuth `state` parameter.

---

## Observability

`compliance_reporter` publishes tier metrics to CloudWatch, and the stack provisions **two alarms per tier** — RED below 80% and YELLOW below 100% — plus a **Lambda-error alarm on every function**, all wired to an SNS topic (ready for a PagerDuty or email subscription). This gives both *business* alarms (a tier slipping out of compliance) and *operational* alarms (a job throwing errors).

---

## Engineering decisions & tradeoffs worth calling out

- **Match on identity-key sets, not a single field.** Cross-system reconciliation is only as good as its join key, and here there *is* no shared key — generating multiple normalized variants and matching on any overlap was the difference between a correct roster and one that silently dropped people.
- **De-dup at read time, not write time.** Keeping every raw badge in DynamoDB (and collapsing on read) preserves the full history while still presenting one credential per person.
- **Server-side CSV parsing for uploads.** The browser could parse the CSV, but keeping one Python parser avoids drift between "how the seed roster was built" and "how uploads are processed."
- **Fallback roster.** Bundling a seed roster means the feature degrades gracefully — the tab is never empty and never 500s on a cold storage path.
- **Isolate the flaky job from the critical one.** Sync (external, slow) and status-recompute/alerting (internal, must-run) stay in separate functions so a Credly outage can't suppress expiry alerts.

---

## Current limitations & next steps

Being honest about the edges:

- **The compliance reporter isn't scheduled.** It's fully built and wired to the alarms, but nothing currently invokes it — so the `CompliancePercentage` metrics aren't being emitted and the tier alarms sit in *insufficient data*. Adding a daily EventBridge rule (or folding it into the existing 7 AM batch) lights them up.
- **Notifications need configuration.** SES `FROM_EMAIL` and the Slack webhook aren't set in the stack, so email/Slack delivery is a no-op until a verified SES identity and webhook are supplied.
- **The per-cert EventBridge Scheduler is disabled and redundant** — best removed in favor of the daily checker.
- **DynamoDB Streams are enabled but unconsumed** — a hook left for future event-driven work.
- **Sync is sequential.** Fine at current scale; a `ThreadPoolExecutor` or an SQS fan-out would keep it well under any timeout as the team grows.
- **Single region (us-east-1)** and manual APN CSV upload — both intentional for a small internal tool.

---

## Takeaway

The whole thing is a few hundred lines of Python Lambdas, one React app, and a CDK stack — no servers, no always-on compute, pay-per-request data. The genuinely hard part wasn't the AWS plumbing; it was **reconciling two systems that describe the same people differently** and presenting partner-tier compliance in a way that's actually actionable: who counts, who's missing, and who's about to expire.
