# Private Integrations

A small Home Assistant custom integration that installs and updates other custom
integrations from **private GitHub repositories**.

It does the job of the
[Marketplace](https://www.home-assistant.io/integrations/marketplace) that Home
Assistant ships since 2026.11, but for repositories the Marketplace cannot
reach: pull a custom integration out of a GitHub repo you own — public or
private — and write it into your `custom_components/` folder. There is no
central catalog; you add repos by their `owner/repo` name.

Useful when you maintain integrations that you do not want to publish (work
projects, personal experiments, betas, internal tools) but still want to deploy
to your Home Assistant instance the same way you would any other community
integration. This is exactly the gap the Marketplace leaves open: it installs
repositories that are public on GitHub, and its GitHub connection does not ask
for access to private ones.

## Features

- Authenticated GitHub access via a Personal Access Token, so private repos work.
- Track a repo by **latest release**, a specific **tag**, a **branch** (e.g.
  `main`), or a pinned **commit SHA**.
- **No background polling** — unlike the Marketplace, Private Integrations never
  reaches out to GitHub on its own. Updates happen when you click **Install /
  update**, on your schedule. Designed for repos you control yourself, where you
  already know when there's something new to pull.
- Path-traversal-safe tarball extraction.
- **A list of your integrations** — every tracked repository with the folder it
  installs into, the version on disk and how it is followed — and a search of
  every repository your token can read, offered as proposals to add with one
  click. Nothing is downloaded until you confirm.

Themes, dashboards, templates, apps, and the other Marketplace categories are
out of scope. This is integrations only.

## Requirements

- Home Assistant **2024.11** or newer. Tested against 2024.11 (the minimum),
  2026.10 and 2026.11.1: the suite runs on the first two and on the version the
  test harness pins, and a real Home Assistant 2026.11.1 is booted for the
  newest release. Home Assistant 2026.x runs on Python 3.14, and so does this
  integration.
- A GitHub Personal Access Token with read access to the repositories you want
  to install.

## Installation

### Marketplace (Home Assistant 2026.11+)

The Marketplace can install this integration itself when its repository is
**public** (the Marketplace does not accept private repositories):

1. Make sure the repository is public.
2. In Home Assistant, open **Settings → Marketplace → Add from link**.
3. Enter `https://github.com/cypherbits/private-integrations`.
4. Select the repository, click **Install**, then restart Home Assistant.

If this repository is private, use the manual method below.

### HACS (Home Assistant 2026.10 and older)

HACS was replaced by the Marketplace in 2026.11, so this only applies to older
releases:

1. In Home Assistant, open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/cypherbits/private-integrations` as an **Integration**
   category.
3. Click **Install** on the new "Private Integrations" entry, then **restart Home
   Assistant**.

When Home Assistant upgrades to 2026.11 it removes HACS. Files that HACS
installed, including this integration, stay in place; update them manually or
reinstall from the Marketplace if the repository is public.

### Manual

This works whether the repository is public or private, and it is the only
option when it is private. Copy
`custom_components/private_integrations/` into your Home Assistant
`config/custom_components/` directory and restart.

## Using it alongside the Marketplace

Home Assistant 2026.11 ships the official **Marketplace**, and both tools write
to `custom_components/`, so keep their responsibilities separate:

- **Marketplace**: public repositories, catalog content, dashboards, themes and
  templates. It checks for updates in the background and creates update entities
  and repairs.
- **Private Integrations**: private repositories, and public ones you prefer to
  pull on demand. The Marketplace cannot add a private repository: custom
  repositories must be public, and its GitHub connection does not request access
  to private repositories.
- **Do not track the same integration in both.** Private Integrations refuses to
  overwrite a folder it did not install, including one installed by the
  Marketplace, but the Marketplace does not know about Private Integrations'
  bookkeeping and may replace those files if you install the same repository
  there.
- Private Integrations refuses to install over the `hacs`, `homeassistant`,
  `marketplace` and `private_integrations` folders.

If you are coming from HACS: Home Assistant removes HACS when it upgrades to
2026.11 and the Marketplace takes over its public content. Private repositories
that HACS used to update with a token are not readable by the Marketplace; add
them here instead.

## Upgrading from Private HACS

The integration used to be called `private_hacs`. The new domain is
`private_integrations`, so Home Assistant treats it as a different integration:
install it as a new one, do not replace the old folder in place.

The upgrade can keep everything:

- The **installed integrations** (their folders and their config entries) are
  not touched.
- When you add **Private Integrations**, the setup flow looks for the old
  `private_hacs` config entry and offers to import its GitHub token.
- On first setup, the old `.storage/private_hacs.repositories` registry is
  copied to `.storage/private_integrations.repositories`, and the
  `.private_hacs.json` markers inside the installed folders are renamed to
  `.private_integrations.json`. That is what keeps those folders updateable and
  removable from the new tool.

Create a Home Assistant backup before starting.

1. Install this integration (Marketplace, HACS on 2026.10 or older, or manual).
   Leave the old `custom_components/private_hacs/` folder in place for now.
2. Go to **Settings → Devices & services → Add integration → Private
   Integrations**. If the old entry is found, the first screen is **Import
   Private HACS**: submit it to reuse the stored token. If the token has
   expired, the normal token form appears and you can paste a new one.
3. Finish the setup and restart Home Assistant when asked.
4. Open **Configure → My integrations** and check that the
   repositories are listed. Run **Install / update** on one of them; if it says
   it is already up to date, the folder was adopted correctly.
5. Remove the old **Private HACS** config entry and the
   `custom_components/private_hacs/` folder. The migration keeps the old
   registry as a backup; delete `.storage/private_hacs.repositories` only after
   you are happy with the result.
6. Update automations and scripts: service names changed from
   `private_hacs.*` to `private_integrations.*`. Pending restart notifications
   from the old name can be dismissed.

Notes:

- Do not remove the old config entry **before** adding the new integration: its
  removal deletes the old registry (the installed files stay). If the entry is
  already gone, add the new integration with a token; the registry is imported
  if the file still exists.
- If a tracked folder has no marker at all, the migration adopts it only when
  its `manifest.json` domain matches the folder. Otherwise the repository is
  listed without files: set its integration folder name and install it again,
  or create `.private_integrations.json` by hand.
- The old `private_hacs` and the new `private_integrations` can be loaded at the
  same time during the transition, but do not manage the same folders with both.
  Remove the old one after checking the import.

If the automatic import cannot run, the two files it uses can be moved by hand
with Home Assistant stopped:

```bash
cp .storage/private_hacs.repositories .storage/private_integrations.repositories
find custom_components -maxdepth 2 -name '.private*hacs*' -print
# rename each marker to .private_integrations.json, keeping its contents
```

## First-time setup

1. **Settings → Devices & services → Add integration → "Private Integrations"**.
2. Paste a GitHub Personal Access Token. Either works:
   - **Fine-grained** PAT (**recommended**) scoped to the specific repos you
     want to install, with **Repository permissions → Contents: Read-only**.
   - **Classic** PAT with the `repo` scope. Note this grants access to *every*
     private repository your account can see, so prefer fine-grained tokens.

   The token is kept in the config entry and stored by Home Assistant in
   `.storage/core.config_entries`, which is **plain JSON** — it is not
   encrypted. Treat Home Assistant backups as secrets and rotate the token if
   one leaks. The token is redacted from the Home Assistant API.

## Adding a repository (UI)

**Settings → Devices & services → Private Integrations → Configure** opens a
menu: **My integrations** (the list), **Add a repository by URL**, **Find
integrations in my GitHub repositories** and **Update the GitHub token**.

### Finding integrations in your account

**Find integrations in my GitHub repositories** asks GitHub for every
repository the token can read, then looks for a
`custom_components/<name>/manifest.json` folder in each one. What it finds is
listed as proposals, with the version in the manifest:

- A **fine-grained PAT** only sees the repositories it was scoped to, so the
  list is exactly that set. A **classic PAT** sees everything the account can
  read, including organisation repositories the account belongs to.
- Repositories already tracked are left out; archived ones are skipped.
- The lookup is capped (about 60 repositories, most recently updated first,
  plus a limit on the manifests it reads), so it answers in seconds instead of
  walking an organisation with a thousand repos. Only when you pick a proposal
  is anything downloaded: the next screen shows the integration, its version
  and how it will be followed, exactly like the manual form.
- A folder that is not an integration — no manifest, or a manifest whose
  `domain` does not match the folder — is not offered. Neither is a repository
  without `custom_components/<name>/`.
- If GitHub cannot answer (bad token, rate limit), the screen says which one it
  was instead of showing an empty list.

### Adding one by hand

1. Pick **"Add a repository by URL"**.
2. Paste the repo URL (`https://github.com/owner/repo`) or `owner/repo`.
3. Choose how to track it. The default is **branch**, leaving the ref empty
   to follow the repository's default branch — which fits a personal "I push
   when it's ready, then click update" workflow. You can also pick latest
   **release**, a specific **tag**, or a pinned **commit** SHA. A tag, branch or
   commit that does not exist is rejected while you are still in the form, by
   name, instead of failing on the next install.
4. Leave **"Install immediately after adding"** checked. The form will both
   register the repo and copy its `custom_components/<name>/` folder into
   place.
5. **Restart Home Assistant** to load the new integration. HA does not
   hot-reload custom integration code — this constraint applies to the
   Marketplace too.

If the install fails, the repository stays tracked and the dialog says why: the
add itself succeeded, and what usually goes wrong — the wrong folder name, a
repository without `custom_components/<integration>/` — can be corrected under
**Configure → My integrations** without adding it again.

## Managing tracked repositories (UI)

**Settings → Devices & services → Private Integrations → Configure → "My
integrations"** lists every tracked repository — one row per integration, with
its folder, the version on disk and how it is followed. Pick one and:

- **Install / update** — re-download the configured ref and replace the
  installed files. The archive is unpacked into a staging folder, validated
  and swapped into place, so a failed download or a malformed repository
  leaves the working version untouched. Restart Home Assistant afterwards to
  load the new code. When the tracked ref already resolves to the commit on
  disk it does nothing, and the dialog says so instead of asking for a restart.
- **Reinstall the same commit (force)** — what the `force` option of the
  services does, for when you want the files rewritten anyway.
- **Change tracking ref and folder** — switch between release, tag, branch or
  commit without reinstalling or losing the tracking history, and fix which
  folder under `custom_components/` the repository installs into. Run
  **Install / update** afterwards to apply the new ref. Once the repository has
  files installed, the folder cannot be changed here: remove the repository
  (with its files) and add it again.
- **Remove from tracking** — stop tracking the repository. Files are deleted
  only if you leave the **Delete installed files** box checked; uncheck it to
  keep the integration running while you move it to another tool.

The screen also says how many folders under `custom_components/` this
integration does not manage — installed by the Marketplace, HACS, Home
Assistant itself or by hand. The discovery marks a proposal whose folder is
already on disk as **already installed**: adding it is refused until that
folder is removed, except when its owner is no longer tracked, the one case a
new repository can take it over.

Installs never overwrite a `custom_components/<name>/` folder that Private
Integrations did not create: integrations installed by the Marketplace, HACS,
Home Assistant itself or by hand are protected, as are the `hacs`,
`homeassistant`, `marketplace` and `private_integrations` folders. A folder is
tied to the repository that installed it (a marker inside the folder records
which one), and two tracked repositories cannot share the same one. The single
exception is a folder whose owner is no longer tracked -- after removing a
repository with **Delete installed files** unchecked -- which a new repository
can take over instead of leaving it unusable.

A folder without that marker is always treated as foreign, even when it looks
like the integration this repository provides: Private Integrations never
overwrites or deletes it. Delete it manually if you want to take it over.

There is no background polling. Private Integrations only contacts GitHub when you
explicitly add, install, or update a repo.

## Service API (optional)

Everything the UI does is also exposed as services if you want to drive it
from automations:

```yaml
service: private_integrations.add_repository
data:
  full_name: https://github.com/cypherbits/my-private-integration
  ref_type: release      # release | tag | branch | commit
  # ref: v1.2.0          # required for tag/commit; blank branch = default branch
  # integration_name: my_integration   # only if the repo has multiple integrations
```

Other services: `private_integrations.install`, `private_integrations.update`,
`private_integrations.remove_repository`.

Options on those services:

- `force: true` on `install` and `update` reinstalls even when the tracked ref
  already resolves to the commit on disk (the default skips it and changes
  nothing).
- `delete_files: false` on `remove_repository` stops tracking the repository and
  leaves `custom_components/<integration>/` alone. Deletion only ever removes a
  folder this repository installed; anything else is refused with an error that
  names its owner, or says that the folder has no ownership marker.

## Repository layout requirements

The repo must contain a `custom_components/<integration_name>/` directory at
its root, the same convention the Marketplace uses. Private Integrations
extracts only that folder, ignoring everything else in the repo.

If a repo contains more than one integration, set `integration_name` when
calling `add_repository` to pick which one to install.

The repository must ship a valid `manifest.json` inside the integration folder
and its `domain` must match the folder name — Home Assistant cannot load an
integration otherwise, so the install is rejected instead of writing files
that would never work.

## Why not just use the Marketplace?

The Marketplace is the right default for public content. Private Integrations
exists because:

- The Marketplace only installs public repositories, and its GitHub connection
  does not request access to private ones.
- It polls in the background and creates update entities; this tool only
  contacts GitHub when you ask it to.
- It is private-first, with plain Home Assistant services and no separate UI
  panel.
- It is small enough to read end-to-end (~2,500 lines, docstrings included) and
  audit before running.
- Private Integrations also works on Home Assistant 2024.11 through 2026.10,
  before the Marketplace existed. HACS covered part of that range and was
  replaced by the Marketplace in 2026.11.

## Development

```bash
# Pick one stack, in a fresh virtual environment:
pip install -r requirements_test.txt      # Home Assistant 2026.10
pip install -r requirements_test_min.txt  # Home Assistant 2024.11

pytest -q
ruff check .
```

Both files are test-only: the integration has no runtime requirements, so Home
Assistant never installs them. `requirements_test.txt` pins the recent Home
Assistant stack used day to day; `requirements_test_min.txt` pins the oldest
release this integration claims to support.

`pytest` runs coverage and fails below 95 %, so a change that stops exercising a
branch breaks the build. The suite covers tarball extraction (including path
traversal, missing manifests and failed upgrades), ref resolution against a
mocked GitHub API, the discovery of integrations in the account, the
config/options flows, the store's handling of a corrupted file, and the
consistency between `strings.json`, its English and Spanish translations,
`services.yaml` and the code.

CI runs ruff, hassfest and the tests twice: against the stack in
`requirements_test.txt` (Home Assistant 2026.10 today) and against the oldest
supported one in `requirements_test_min.txt`. The second run is what catches a
dependency on a core API that only exists in recent versions; the first catches
one that no longer exists.

There is a third job for the **latest** Home Assistant, which the test harness
does not reach yet: `scripts/smoke_test.py` boots the Home Assistant installed in
the interpreter, with this integration in its `custom_components/`, and walks the
whole surface — config flow, options flow, the list, the discovery, install,
restart notification, removal, unload. Run it locally against any version with:

```bash
pip install homeassistant          # whatever version you want to check
python scripts/smoke_test.py
```

Home Assistant installs the requirements of its own integrations on start, and
the script lets it: without them (no frontend, no TurboJPEG) the bootstrap falls
into recovery mode, where custom integrations are not loaded at all and every
check fails with `IntegrationNotFound`. `--skip-pip` skips that step when the
interpreter already has them.
