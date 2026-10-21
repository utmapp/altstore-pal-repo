---
name: publish-adp
description: Publish a notarized UTM SE build to the AltStore PAL source (pal.getutm.app) from its App Store Connect Alternative Distribution Package (ADP) ID — fetches the ADP via AltStore, updates adp/ + config.json, pushes + tags, uploads the IPAs as the latest GitHub release, and verifies every URL. Use when asked to publish/submit/update UTM SE on AltStore PAL.
argument-hint: <ADP ID>
---

# Publish an ADP to AltStore PAL

Input: the ADP ID (a UUID from App Store Connect for a build approved via Notarization). If it was not given, ask for it.

## How this repo is served

- GitHub Pages serves this repo at `https://pal.getutm.app` (`CNAME`): `config.json` is the AltStore PAL source, and `adp/manifest.json` + `adp/signature` are the ADP metadata. The source's `downloadURL` is `https://pal.getutm.app/adp`.
- The IPAs are too big for git. A Cloudflare rule redirects `pal.getutm.app/adp/<dir>/<name>.ipa` → `github.com/utmapp/altstore-pal-repo/releases/latest/download/<name>.ipa`, so **the latest GitHub release must hold exactly the assets the served manifest references**. Publishing therefore has to flip the manifest and the latest release together (step 5).
- `adp/variant/` and stray `.DS_Store` files in the working tree are local leftovers; never commit them. Never `git add -A` / `git add .`.

All commands run from the repo root. `$SKILL` is this skill's directory (`.claude/skills/publish-adp`).

## 1. Fetch the ADP

```sh
python3 $SKILL/publish_adp.py fetch <ADP_ID>
```

Asks `api.altstore.io` for the ADP status, submits it for processing (`POST /adps`) only if AltStore hasn't processed it yet, waits for `success`, downloads the zip (to `$TMPDIR/altstore-pal/`, reused if already there and valid), and extracts it. It prints `adpDir`, `version`, `buildVersion`, `minOS` and the asset count. Check that `bundleId` is `com.utmapp.UTM-SE` and `version` is what the user expects. Let `V` be the version and `TAG=v$V`.

## 2. Pre-flight

- `git status`, `git fetch`, and make sure `main` is checked out and not behind `origin/main`.
- `git ls-remote --tags origin $TAG` and `gh release view $TAG` must both find nothing. If a release/tag exists, stop and ask (it may be a half-finished previous run — see Recovery).

## 3. Release notes → user checkpoint

Draft the version's `localizedDescription` from the UTM release: `gh release view $TAG --repo utmapp/UTM --json body --jq .body`. Follow the style of previous entries (`git log -p -- config.json`): a Markdown bullet list (`* ...`) of the changes that matter on iOS — take the `## Changes (v$V)` section (plus intermediate versions since the last PAL release if they never shipped here, by reading the old `version` in `config.json`), drop `(macOS…)`-only items, and strip the `(iOS)` prefix. Add the `## Highlights` bullets that apply to iOS for a major/minor release. Write it to a temp file.

Then run:

```sh
python3 $SKILL/publish_adp.py update <adpDir> --notes <notes-file>
git diff --stat; git diff config.json
```

`update` copies `manifest.json` and `signature` byte-for-byte into `adp/` (AltStore rejects any change to them — never reformat or re-serialize them), and in `config.json` sets the version entry's `version`, `buildVersion`, `date` (today), `localizedDescription`, `size` (size of the largest IPA in the ADP), plus the app's `minOSVersion` from the manifest. The source keeps a single version entry because `pal.getutm.app/adp` only ever serves the latest ADP.

**Show the user the version/build/size, the notes, and the `config.json` diff, and get approval before going on.** Everything after this is public and hard to undo.

## 4. Upload the release as a draft

Drafts don't change `releases/latest`, so the slow 1+ GB upload happens while nothing is live:

```sh
gh release create $TAG --draft --title $TAG --notes "" --target main \
  $(python3 $SKILL/publish_adp.py assets <adpDir>)
```

`assets` lists every `assetPath` in the manifest (variants *and* deltas), uploaded under its basename. If the upload is interrupted, re-run the missing ones with `gh release upload $TAG <files> --clobber`. Then confirm the draft's asset names and sizes match the local files exactly:

```sh
gh release view $TAG --json assets --jq '.assets[] | "\(.name) \(.size)"' | sort
for f in $(python3 $SKILL/publish_adp.py assets <adpDir>); do echo "$(basename $f) $(stat -f %z $f)"; done | sort
```

## 5. Commit, push, tag, publish — back to back

Run these together without pausing, to keep the window where the served manifest and the latest release disagree as short as possible:

```sh
git add adp/manifest.json adp/signature config.json
git commit -m "Update for $TAG" -m "Assisted-by: Claude:<model-id>"
git push origin main
git tag $TAG && git push origin $TAG
gh release edit $TAG --draft=false --latest
```

Pass `--latest` explicitly (GitHub otherwise picks "latest" by its own rules). Don't add `Co-authored-by` trailers or session links.

## 6. Verify

```sh
python3 $SKILL/publish_adp.py verify --adp-dir <adpDir>
```

Retries for up to 10 minutes (`--wait`) to ride out the Pages deploy and CDN caching. It passes only when:
- `https://pal.getutm.app/config.json` → 200 and serves the new `version`/`buildVersion`;
- `/adp/manifest.json` and `/adp/signature` → 200 and byte-identical to the committed files;
- every asset in the manifest → 200 through the Cloudflare redirect, with `Content-Length` equal to the local file;
- the redirect lands on `/releases/download/$TAG/` (a bare 200 could still be the previous release).

Report the result to the user with the release URL. If the Pages deploy is slow, check `gh run list --workflow pages-build-deployment` before re-running verify.

## Recovery

- Failure before step 5: nothing is public. Delete the draft (`gh release delete $TAG --yes`) if needed and `git checkout -- adp/manifest.json adp/signature config.json`.
- Failure during step 5 or verify: don't delete the published release or force-push. Find which half is missing (pushed commit vs. published latest release vs. assets) and finish it, then re-run verify. Report what happened to the user.
