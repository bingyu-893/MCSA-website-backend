# MCSA content backend

Python / Flask / Waitress with an independent Chinese CMS, persistent SQLite content and media uploads. This repository runs on a server such as an Alibaba Cloud instance; GitHub Pages does not run it.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python start.py
```

Windows users can run `START-WINDOWS.bat`. First launch asks for the administrator password. Open `/admin.html`; username is `admin`.

## Connect the public website

Set `deployment.json`:

- `frontendUrl`: complete website URL, including the GitHub Pages repository path.
- `backendUrl`: this backend's HTTPS origin, without `/api` or `/admin.html`.

In the public website's `assets/config.js`, set `apiBase` to the `backendUrl` above followed by `/api`.

Only the public `GET /api/site` endpoint supports cross-origin reads. It returns current published content and absolute CMS image URLs. Admin writes require login and CSRF. Upload URLs are publicly retrievable when known; do not upload confidential material.

## Persistent data

`MCSA_INSTANCE` selects the data directory. Default: local `instance/`. The supplied systemd service uses `/var/lib/mcsa-backend`. SQLite stores content and accounts; `credentials.json` retains the session secret and legacy administrator password hash. Keep credentials, SQLite and `media/` outside Git. Do not replace this directory when updating code. Use `python start.py --reset-password` to reset the local super administrator password; it updates the account record and invalidates existing sessions.

`seed.json` is used only for a new database. Existing fifth-version content can be retained by copying the complete stopped `instance` contents into the selected data directory.

## Deployment

Templates are in `deploy/`. Use HTTPS with Nginx proxying to Waitress on `127.0.0.1:8000`. Apply the supplied proxy fragment to the correct site's HTTPS server block, not to the entire Nginx configuration. Configure TLS and DNS separately.

Optional environment overrides: `MCSA_FRONTEND_URL`, `MCSA_PUBLIC_URL`, `MCSA_ALLOWED_ORIGINS` (comma-separated origins), `MCSA_INSTANCE`, `MCSA_HOST`, `PORT`. Translation keys stay on the server; `.env.example` is a template, not automatically loaded by Python.

## Checks

```bash
python -m unittest discover -s tests -v
```

## English translation and review

Saving Simplified Chinese generates Traditional Chinese locally and queues English
translation in SQLite. `start.py` starts the worker automatically; custom servers
must call `app.extensions["translations"].start()` once.

Set `AZURE_TRANSLATOR_KEY` and `AZURE_TRANSLATOR_REGION` on the server, then restart.
`.env.example` is not loaded automatically. Keep keys out of frontend config and Git.

In **英文校对**, refresh translation status, review candidates, then **保存英文草稿**
or **批准并发布**. Drafts are private. New fields may publish machine English;
approved and legacy English remain visible until a replacement is approved.
Reviewers need `site.read` and `site.write`. Save Chinese edits first; on a 409
conflict, refresh and review again.

Enable confirmed names in **名称术语表** and preserve placeholders such as `{name}`.
Use **预览内容** to compare Chinese, public English and candidates. Correct any
reported configuration or translation errors before using **重新翻译**. Fields
without usable English fall back to Chinese with “Translation pending”.

Deploy the backend/admin and official frontend together, then verify translation
and approval with a valid Azure key. Backups include private review data and the
glossary; stop the server before restoring.

### Browser regression checks

Start the temporary fixture from this backend directory:

```sh
python tests/translation_browser_server.py
```

Then run from the official frontend directory with Playwright available:

```sh
node --test tests/translation-browser.test.cjs
```

The fixture uses a temporary database and fake translator on local ports 8765/8766.
`MCSA_BROWSER_EXECUTABLE` selects Chrome/Edge; `NODE_PATH` locates shared Playwright.
Stop the fixture after testing and never deploy it.

## Accounts and permissions

Login and sessions use the SQLite `users` table. The existing administrator is migrated as a super administrator. In the CMS, only super administrators can open **账号与权限** to create, edit, or disable accounts and assign permissions. The same restriction applies to `GET/POST /api/admin/accounts` and `PUT /api/admin/accounts/<id>`; all writes require CSRF. Password hashes are never returned by these APIs.

Ordinary administrators can be assigned `site.read`, `site.write`, `media.upload`, and `backup.download`. Editing or uploading requires read permission. These permissions are checked on the backend for every request and again against the database for existing sessions. Changing an account revokes its current sessions. At least one active super administrator must remain. These permissions cover the entire CMS content tree rather than individual sections. Supabase authentication is not used.
Author：Guo Yu


