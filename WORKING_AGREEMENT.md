# Working Agreement

This document defines default engineering practices for this repository.

## 1) Language and Runtime Defaults

- WhatsApp bot runtime: Node.js (project/bot).
- Intelligence/API layer: Python FastAPI (project/backend).
- Python environment: use conda env `wa-data` for all Python commands unless explicitly overridden.

## 2) Coding Standards

### Python

- Prefer explicit, typed function signatures for new logic-heavy functions.
- Keep endpoint handlers thin; move reusable logic into helpers.
- Use small pure functions for extraction, scoring, and classification.
- Fail loudly with clear error messages when assumptions are violated.
- Avoid introducing packages when standard library is sufficient.

### JavaScript (Bot)

- Keep async flows explicit with try/catch around network and WhatsApp API calls.
- Normalize external payloads defensively before backend submission.
- Log key operational events with concise machine-readable objects.

## 3) Git Workflow

- Branch naming:
  - feat/<short-topic>
  - fix/<short-topic>
  - chore/<short-topic>
  - docs/<short-topic>
- Commit style: small, focused, frequent commits (one logical change per commit).
- Commit message style:
  - feat: add outbound send queue polling
  - fix: handle missing reaction msg id
  - docs: update user.env setup guide
- Avoid large mixed commits that combine unrelated backend, bot, and docs work.

## 4) Testing Expectations

- Unit tests are optional and added when logic complexity justifies them.
- Minimum required checks for each meaningful change:
  - Python syntax check for touched backend files.
  - JavaScript syntax check for touched bot files.
  - Manual endpoint smoke test for changed API routes.
- Add automated tests first for high-risk logic (dedupe, ranking, filtering, reactions).

## 5) Dependency Policy

- Keep dependencies minimal and purposeful.
- Prefer mature, widely used libraries over niche packages.
- Pin versions in requirements and package manifests where practical.
- Remove unused dependencies promptly.

## 6) Configuration and Secrets

- Use user.env for local configuration.
- Never commit real secrets or tokens.
- Commit only sanitized examples (user.env.example).
- Keep all environment setup and update steps reflected in README.

## 7) Data Governance Defaults

- Collect only data required for product features.
- Keep raw message storage auditable; keep derived features separate and explicit.
- Treat user identifiers and message content as sensitive.
- Avoid exporting raw sensitive data in logs or public reports.
- Add retention/deletion policy decisions before production rollout.

## 8) Product Taxonomy (V1)

Messages should use one primary category plus zero or more tags. Prefer intent over source format.

Primary categories:
- opportunities
- startup-funding-news
- events-hackathons-meetups
- learning-and-research
- open-source-and-repos
- tools-and-libraries
- product-launches
- articles-and-industry-news
- ai-ml
- facts-and-insights

Supporting tags:
- source-twitter
- source-github
- source-youtube
- funding
- internship
- referral
- hackathon

Dynamic taxonomy:
- New category proposals are generated from repeated low-confidence/fallback content.
- Proposals require explicit review approval before becoming active categories.

## 9) Definition of Done

A feature is done when all are true:
- Behavior implemented and manually validated.
- README updated for setup and usage changes.
- TODO files updated (completed items checked; follow-ups captured).
- No new syntax or lint errors in touched files.
- Security/privacy impact considered for new data fields.

## 10) TODO Hygiene

- Keep TODOs categorized by domain and easy to scan.
- Use status markers:
  - [ ] not started
  - [~] in progress
  - [x] completed
- Keep one master TODO index at repository root.
- Link deeper domain TODO files from the master index.
