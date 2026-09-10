"""
Download the third-party JS/WASM the in-browser STEP viewer needs into
`microwave_package/vendor/` (git-ignored). Run once, with internet:

    python -m microwave_package.fetch_vendor          # skip files already there
    python -m microwave_package.fetch_vendor --force  # re-download everything

Nothing else in the package imports this. The web UI checks whether these
files exist and simply hides the "3D view" button when they do not, so
skipping this step leaves the rest of the app working exactly as before.

Everything is pinned and served locally afterwards (see webapp.py's
`/vendor/<name>` route), so the viewer keeps working with no internet --
the same reason plotly.js is served from the installed package rather
than a CDN.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request

from . import paths

# name in vendor/  ->  URL to fetch it from. Versions are pinned; bump
# them here deliberately. occt-import-js.js loads occt-import-js.wasm at
# runtime by name, so the two must stay from the same release.
THREE_VERSION = "0.169.0"
OCCT_VERSION = "0.0.23"

FILES = {
    "three.module.min.js": f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.min.js",
    "OrbitControls.js": f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/controls/OrbitControls.js",
    "occt-import-js.js": f"https://unpkg.com/occt-import-js@{OCCT_VERSION}/dist/occt-import-js.js",
    "occt-import-js.wasm": f"https://unpkg.com/occt-import-js@{OCCT_VERSION}/dist/occt-import-js.wasm",
}


def fetch(force: bool = False) -> int:
    paths.VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for name, url in FILES.items():
        target = paths.VENDOR_DIR / name
        if target.exists() and not force:
            print(f"  keep  {name} ({target.stat().st_size / 1024:.0f} KB, already present)")
            continue
        print(f"  get   {name}  <-  {url}")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 -- pinned https URLs
                data = response.read()
        except OSError as exc:
            print(f"        FAILED: {exc}", file=sys.stderr)
            return 1
        target.write_bytes(data)
        print(f"        saved {len(data) / 1024:.0f} KB")
        fetched += 1
    print(f"\nvendor/ is ready ({fetched} file(s) downloaded). The '3D view' button will now appear.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="re-download files that are already present")
    raise SystemExit(fetch(force=parser.parse_args().force))


if __name__ == "__main__":
    main()
