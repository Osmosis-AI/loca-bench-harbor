#!/usr/bin/env bash
# Regenerate this repository for one runtime-image digest. Does not commit.
set -euo pipefail

image=""; version=""; harbor_src=""; loca_src=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime-image) image="$2"; shift 2 ;;
    --dataset-version) version="$2"; shift 2 ;;
    --harbor-src) harbor_src="$2"; shift 2 ;;
    --loca-src) loca_src="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$image" ] || [ -z "$version" ] || [ -z "$harbor_src" ] || [ -z "$loca_src" ]; then
  echo "usage: $0 --runtime-image <ref@sha256:...> --dataset-version <x.y.z>" \
       "--harbor-src <path> --loca-src <path>" >&2
  exit 2
fi
case "$image" in
  *@sha256:*) ;;
  *) echo "--runtime-image must pin a digest (…@sha256:…), got: $image" >&2; exit 2 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$harbor_src/adapters/loca-bench"

uv run --project "$adapter" loca-bench generate --source-dir "$loca_src" \
  --runtime-image "$image" --dataset-version "$version" --output-dir "$root"
uv run --project "$adapter" loca-bench validate --dataset-dir "$root" \
  --runtime-image "$image"

cat <<NEXT
Generated and validated. Next, from $root:

  git add -A && git commit -m "Release loca-bench $version"
  git tag -a "v$version" -m "loca-bench $version"
  git push origin main && git push origin "v$version"
NEXT
