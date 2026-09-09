# loca-bench-harbor

Generated Harbor tasks for [LOCA-bench](https://github.com/hkust-nlp/LOCA-bench),
one Harbor task per upstream configuration entry
(15 families x 7 EDL levels x 5 seeds = 525).

Generated artifacts only; the generator lives in `Osmosis-AI/harbor` under
`adapters/loca-bench`. Nothing here may be hand-edited: CI re-validates every
tree against the adapter.

## Status

`v1.3.0` pins `ghcr.io/osmosis-ai/loca-bench-runtime@sha256:8c4e4b61fd1450440620bc998353616d6d8781fc9f8e990bf53b1bb5c395e7df` (linux/amd64 + linux/arm64), built from Harbor commit `ae9f2b9e9af74d3a351cd8c00770b14580209c69` by the [`loca-runtime-ae9f2b9e9af7` release workflow](https://github.com/Osmosis-AI/harbor/actions/runs/34307471645). The manifest records harness `1.3.0+loca.8b6fac49` and the same 525 upstream task configurations as `v1.2.1`.

The runtime adds native OpenAI Responses with complete reasoning and function-call state replay, while retaining Chat Completions. API format is selected independently of reasoning effort. Use the matching Harbor controller from [Harbor PR #21](https://github.com/Osmosis-AI/harbor/pull/21) or a later commit containing it. Updating Harbor alone cannot add Responses support to the older task images.

Task content, scoring rules, per-EDL timeouts, and the existing ReAct profile remain unchanged from `v1.2.1`. Earlier tags remain immutable. `v1.1.0` and `v1.0.0` use the earlier grading semantics and are not comparable across the model-caused API termination change introduced in `v1.2.0`.

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

1. Release the runtime image from `Osmosis-AI/harbor`: tag the adapter commit
   `loca-runtime-<sha12>` and push the tag. The workflow
   `.github/workflows/loca-bench-runtime-image.yml` builds linux/amd64 and
   linux/arm64 natively, pushes the multi-arch tag and prints the index digest
   in its summary.
2. Take that digest, or resolve it: `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'`.
3. `scripts/release.sh --runtime-image <ref>@sha256:<digest> --dataset-version <x.y.z> --harbor-src <harbor checkout> --loca-src <pinned LOCA-bench checkout>`
   (this runs `loca-bench generate` then `loca-bench validate`).
4. `git add -A && git commit -m "Release loca-bench <x.y.z>"`
5. `git tag -a v<x.y.z> -m "loca-bench <x.y.z>"`
6. `git push origin main && git push origin v<x.y.z>`

Tags are immutable. A corrected release is a new version, never a moved tag.

## License

MIT. Task metadata derives from LOCA-bench; see `LICENSE`.
