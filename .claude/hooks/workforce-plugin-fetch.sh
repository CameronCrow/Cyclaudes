#!/bin/bash
# SessionStart hook: on a cloud/remote session, fetch + link any GitHub-repo-backed
# plugin marketplaces this repo's .claude/settings.json declares enabled.
#
# Why this exists: cloud containers start with an empty ~/.claude/plugins -- the
# cloud harness does not auto-fetch a marketplace source the way the local `claude`
# CLI does on `/plugin install`. This hook does that fetch by hand, generically --
# it reads enabledPlugins + extraKnownMarketplaces itself, so any future plugin
# enabled the same way is picked up with no changes to this script.
#
# Local machines never run this: they already get skills via a junction/copy
# install (see INSTALL.md), and this hook is gated to remote sessions only.
set -euo pipefail

if [ -z "${CLAUDE_CODE_REMOTE:-}" ]; then
  exit 0
fi

SETTINGS="${CLAUDE_PROJECT_DIR:-$(pwd)}/.claude/settings.json"
[ -f "$SETTINGS" ] || exit 0

mkdir -p "$HOME/.claude/plugins/marketplaces" "$HOME/.claude/skills"

python3 - "$SETTINGS" <<'PYEOF'
import json, os, subprocess, sys

settings_path = sys.argv[1]
with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)

enabled = settings.get("enabledPlugins", {})
marketplaces = settings.get("extraKnownMarketplaces", {})

home = os.path.expanduser("~")
mp_root = os.path.join(home, ".claude", "plugins", "marketplaces")
skills_root = os.path.join(home, ".claude", "skills")

seen = set()
for plugin_key, is_on in enabled.items():
    if not is_on or "@" not in plugin_key:
        continue
    marketplace_name = plugin_key.split("@", 1)[1]
    if marketplace_name in seen:
        continue
    seen.add(marketplace_name)

    mp = marketplaces.get(marketplace_name)
    if not mp:
        print(f"[workforce-plugin-fetch] {marketplace_name}: no matching extraKnownMarketplaces entry, skipping")
        continue
    source = mp.get("source", {})
    if source.get("source") != "github":
        print(f"[workforce-plugin-fetch] {marketplace_name}: non-github source not supported by this hook, skipping")
        continue
    repo = source.get("repo")
    if not repo:
        continue

    dest = os.path.join(mp_root, marketplace_name)
    if os.path.isdir(os.path.join(dest, ".git")):
        subprocess.run(["git", "-C", dest, "pull", "--ff-only"], check=False)
        print(f"[workforce-plugin-fetch] {marketplace_name}: updated existing clone at {dest}")
    else:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             f"https://github.com/{repo}.git", dest],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[workforce-plugin-fetch] {marketplace_name}: clone FAILED: {result.stderr.strip()}")
            continue
        subprocess.run(["git", "-C", dest, "sparse-checkout", "set", "skills"], check=False)
        print(f"[workforce-plugin-fetch] {marketplace_name}: cloned (sparse: skills/ only) to {dest}")

    skills_src = os.path.join(dest, "skills")
    skills_dst = os.path.join(skills_root, marketplace_name)
    if os.path.isdir(skills_src) and not os.path.exists(skills_dst):
        try:
            os.symlink(skills_src, skills_dst, target_is_directory=True)
            print(f"[workforce-plugin-fetch] {marketplace_name}: linked skills at {skills_dst}")
        except OSError as e:
            import shutil
            shutil.copytree(skills_src, skills_dst)
            print(f"[workforce-plugin-fetch] {marketplace_name}: symlink unavailable ({e}); copied skills to {skills_dst} instead")
    elif os.path.exists(skills_dst):
        print(f"[workforce-plugin-fetch] {marketplace_name}: {skills_dst} already exists, left as-is")

print("[workforce-plugin-fetch] workforce plugin bootstrap done")
PYEOF
