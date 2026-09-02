# loca-bench-harbor

Generated Harbor tasks for [LOCA-bench](https://github.com/hkust-nlp/LOCA-bench),
one Harbor task per upstream configuration entry
(15 families x 7 EDL levels x 5 seeds = 525).

Generated artifacts only; the generator lives in `Osmosis-AI/harbor` under
`adapters/loca-bench`. Nothing here may be hand-edited: CI re-validates every
tree against the adapter.

## Status

The skeleton is committed without `tasks/`, `registry.json` or `manifests/`:
no runtime-image digest has been published yet, and a tree generated against a
mutable tag must never land on `main` or a release tag. The first release
commit adds all three at once.

## Layout

```
registry.json                     seven dataset entries; task items are {name, path}
tasks/<task_id>/                  task.toml, instruction.md, tests/test.sh, environment/Dockerfile
manifests/loca-bench-<version>.json  upstream pin, runtime image digest, matrix
```

## Datasets

`loca-bench` 525, `loca-bench-core` 225, `loca-bench-8k` / `-64k` / `-128k` /
`-256k` 75 each, `loca-bench-smoke` 8.

## Consuming

```bash
harbor run --repo Osmosis-AI/loca-bench-harbor@v<version> --dataset loca-bench@<version>
```

Registry task entries carry only `name` and `path`; Harbor binds `git_url` and
the resolved commit at resolve time. The platform sets
`harbor_repo="Osmosis-AI/loca-bench-harbor@v<version>"` and
`harbor_dataset="loca-bench@<version>"`.

## Release procedure

The order is one-way. Never generate against a mutable tag.

1. Build the runtime image from `Osmosis-AI/harbor` `adapters/loca-bench/runtime/Dockerfile` and push it.
2. Resolve its immutable digest: `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'`.
3. `scripts/release.sh --runtime-image <ref>@sha256:<digest> --dataset-version <x.y.z> --harbor-src <harbor checkout> --loca-src <pinned LOCA-bench checkout>`
   (this runs `loca-bench generate` then `loca-bench validate`).
4. `git add -A && git commit -m "Release loca-bench <x.y.z>"`
5. `git tag -a v<x.y.z> -m "loca-bench <x.y.z>"`
6. `git push origin main && git push origin v<x.y.z>`

Tags are immutable. A corrected release is a new version, never a moved tag.

## License

MIT. Task metadata derives from LOCA-bench; see `LICENSE`.
