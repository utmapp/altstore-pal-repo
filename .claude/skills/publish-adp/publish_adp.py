#!/usr/bin/env python3
"""Helper for the publish-adp skill. Standard library only.

  fetch  ADP_ID                 process (if needed) + download + extract the ADP
  update ADP_DIR --notes FILE   copy manifest/signature into adp/, update config.json
  assets ADP_DIR                print the release asset files (one path per line)
  verify                        check pal.getutm.app serves the new version end to end
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ALTSTORE_API = "https://api.altstore.io"
SITE = "https://pal.getutm.app"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORKDIR = Path(tempfile.gettempdir()) / "altstore-pal"
UA = {"User-Agent": "utm-publish-adp/1.0"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http(method, url, body=None, timeout=60):
    headers = dict(UA)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.headers, resp.read() if method != "HEAD" else b""
    except urllib.error.HTTPError as e:
        return e.code, url, e.headers, e.read()


def adp_status(adp_id):
    status, _, _, body = http("GET", f"{ALTSTORE_API}/adps/{adp_id}")
    if status == 404:
        return None
    if status != 200:
        sys.exit(f"GET /adps/{adp_id} -> HTTP {status}: {body[:500]!r}")
    return json.loads(body)


def zip_ok(path):
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and "manifest.json" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def safe_extract(zip_path, dest):
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if dest not in target.parents and target != dest:
                sys.exit(f"refusing to extract {name!r} outside {dest}")
        z.extractall(dest)


def cmd_fetch(args):
    adp_id = args.adp_id
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    zip_path = workdir / f"{adp_id}.zip"
    out_dir = workdir / adp_id

    if zip_path.exists() and zip_ok(zip_path):
        log(f"reusing {zip_path}")
    else:
        info = adp_status(adp_id)
        # Only submit for processing when AltStore hasn't already processed it
        # (ASC marketplace notifications normally do this automatically).
        if info is None or info.get("status") not in ("success", "pending", "processing") or info.get("downloadExpired"):
            log(f"submitting ADP {adp_id} for processing (status: {info and info.get('status')})")
            status, _, _, body = http("POST", f"{ALTSTORE_API}/adps", {"adpID": adp_id})
            if status not in (200, 201, 202):
                sys.exit(f"POST /adps -> HTTP {status}: {body[:500]!r}")
        deadline = time.time() + args.timeout
        while True:
            info = adp_status(adp_id)
            st = info and info.get("status")
            if st == "success" and info.get("downloadURL") and not info.get("downloadExpired"):
                break
            if st not in (None, "pending", "processing", "success"):
                sys.exit(f"ADP processing failed: {json.dumps(info)}")
            if time.time() > deadline:
                sys.exit(f"timed out waiting for ADP; last status: {json.dumps(info)}")
            log(f"status: {st}; waiting...")
            time.sleep(30)
        # downloadURL is a short-lived signed URL: use it immediately.
        log(f"downloading {zip_path}")
        tmp = zip_path.with_suffix(".zip.part")
        req = urllib.request.Request(info["downloadURL"], headers=UA)
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        if not zip_ok(tmp):
            sys.exit(f"downloaded zip {tmp} is corrupt")
        tmp.rename(zip_path)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    safe_extract(zip_path, out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text())
    print(json.dumps({
        "adpDir": str(out_dir),
        "bundleId": manifest["bundleId"],
        "version": manifest["shortVersionString"],
        "buildVersion": manifest["bundleVersion"],
        "minOS": manifest["minimumSystemVersions"].get("ios"),
        "assets": len(list(asset_paths(manifest))),
    }, indent=2))


def asset_paths(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "assetPath" and isinstance(v, str):
                yield v
            else:
                yield from asset_paths(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from asset_paths(item)


def release_assets(adp_dir):
    manifest = json.loads((adp_dir / "manifest.json").read_text())
    files = []
    for rel in sorted(set(asset_paths(manifest))):
        path = adp_dir / rel
        if not path.is_file():
            sys.exit(f"manifest references missing file {rel}")
        files.append(path)
    names = [p.name for p in files]
    if len(names) != len(set(names)):
        sys.exit("asset basenames collide; they cannot share one release")
    return files


def cmd_assets(args):
    for path in release_assets(Path(args.adp_dir)):
        print(path)


def cmd_update(args):
    adp_dir = Path(args.adp_dir)
    manifest = json.loads((adp_dir / "manifest.json").read_text())
    notes = Path(args.notes).read_text().strip()
    if not notes:
        sys.exit("release notes are empty")

    config_path = REPO_ROOT / "config.json"
    config = json.loads(config_path.read_text())
    apps = [a for a in config["apps"] if a["bundleIdentifier"] == manifest["bundleId"]]
    if len(apps) != 1:
        sys.exit(f"expected one app {manifest['bundleId']} in config.json")
    app = apps[0]
    if app.get("marketplaceID") != manifest["appleItemId"]:
        sys.exit(f"marketplaceID {app.get('marketplaceID')} != appleItemId {manifest['appleItemId']}")

    # Byte-for-byte: AltStore rejects any change to these files.
    shutil.copyfile(adp_dir / "manifest.json", REPO_ROOT / "adp" / "manifest.json")
    shutil.copyfile(adp_dir / "signature", REPO_ROOT / "adp" / "signature")

    size = max(p.stat().st_size for p in release_assets(adp_dir))
    min_os = manifest["minimumSystemVersions"].get("ios")
    if min_os:
        app["minOSVersion"] = min_os
    # pal.getutm.app/adp only ever serves the latest ADP, so keep a single entry.
    version = app["versions"][0]
    version.update({
        "version": manifest["shortVersionString"],
        "date": args.date or datetime.date.today().isoformat(),
        "localizedDescription": notes,
        "size": size,
        "buildVersion": manifest["bundleVersion"],
    })
    del app["versions"][1:]
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: version[k] for k in ("version", "buildVersion", "size", "date")} | {"minOSVersion": app.get("minOSVersion")}, indent=2))


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{' — ' + detail if detail else ''}")
    return ok


def release_redirect(url):
    """Return the GitHub release download URL that pal.getutm.app redirects an asset to."""
    hops = []

    class Record(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            hops.append(newurl)
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        urllib.request.build_opener(Record).open(req, timeout=60).close()
    except urllib.error.HTTPError:
        pass
    return next((h for h in hops if "/releases/download/" in h), None)


def verify_once(tag, manifest_bytes, signature_bytes, expect_sizes):
    manifest = json.loads(manifest_bytes)
    bust = f"?t={int(time.time())}"
    ok = True

    status, _, _, body = http("GET", f"{SITE}/config.json{bust}")
    served = {}
    if status == 200:
        try:
            app = next(a for a in json.loads(body)["apps"] if a["bundleIdentifier"] == manifest["bundleId"])
            served = app["versions"][0]
        except (ValueError, StopIteration, KeyError):
            pass
    ok &= check("config.json", status == 200 and served.get("version") == manifest["shortVersionString"]
                and served.get("buildVersion") == manifest["bundleVersion"],
                f"HTTP {status}, version {served.get('version')} ({served.get('buildVersion')})")

    for rel, local in (("adp/manifest.json", manifest_bytes), ("adp/signature", signature_bytes)):
        status, _, _, body = http("GET", f"{SITE}/{rel}{bust}")
        ok &= check(rel, status == 200 and body == local,
                    f"HTTP {status}, {'matches' if body == local else 'differs from'} local")

    for rel, size in expect_sizes.items():
        status, _, headers, _ = http("HEAD", f"{SITE}/adp/{rel}")
        length = int(headers.get("Content-Length", -1)) if headers else -1
        ok &= check(rel, status == 200 and size in (None, length), f"HTTP {status}, {length} bytes (want {size})")

    # A 200 alone could come from the previous latest release; confirm the redirect moved.
    if expect_sizes:
        target = release_redirect(f"{SITE}/adp/{next(iter(expect_sizes))}")
        ok &= check(f"redirect → {tag}", bool(target) and f"/releases/download/{tag}/" in target,
                    target or "no release redirect")
    return ok


def cmd_verify(args):
    manifest_bytes = (REPO_ROOT / "adp" / "manifest.json").read_bytes()
    signature_bytes = (REPO_ROOT / "adp" / "signature").read_bytes()
    manifest = json.loads(manifest_bytes)
    tag = f"v{manifest['shortVersionString']}"
    if args.adp_dir:
        adp_dir = Path(args.adp_dir)
        expect_sizes = {p.relative_to(adp_dir).as_posix(): p.stat().st_size for p in release_assets(adp_dir)}
    else:
        expect_sizes = {v["assetPath"]: v["variantDetails"]["compressedSize"]
                        for v in manifest.get("variants", []) if "assetPath" in v}
        expect_sizes.update({rel: None for rel in asset_paths(manifest) if rel not in expect_sizes})

    deadline = time.time() + args.wait
    attempt = 1
    while True:
        print(f"Attempt {attempt}:")
        if verify_once(tag, manifest_bytes, signature_bytes, expect_sizes):
            print(f"\nAll {3 + len(expect_sizes)} URLs resolve with HTTP 200 and serve {tag}.")
            return
        if time.time() > deadline:
            sys.exit("\nverification failed")
        attempt += 1
        time.sleep(30)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("adp_id")
    f.add_argument("--workdir", default=os.environ.get("ADP_WORKDIR", DEFAULT_WORKDIR))
    f.add_argument("--timeout", type=int, default=1800, help="seconds to wait for processing")
    f.set_defaults(func=cmd_fetch)
    u = sub.add_parser("update")
    u.add_argument("adp_dir")
    u.add_argument("--notes", required=True, help="file with the version's localizedDescription")
    u.add_argument("--date", help="YYYY-MM-DD (default: today)")
    u.set_defaults(func=cmd_update)
    a = sub.add_parser("assets")
    a.add_argument("adp_dir")
    a.set_defaults(func=cmd_assets)
    v = sub.add_parser("verify")
    v.add_argument("--adp-dir", help="extracted ADP, to compare exact asset sizes")
    v.add_argument("--wait", type=int, default=600, help="seconds to keep retrying for deploy/CDN lag")
    v.set_defaults(func=cmd_verify)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
