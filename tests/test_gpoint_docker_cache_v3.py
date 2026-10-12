"""Cheap invariants for V3's exact-SHA Docker layer cache separation."""
import pathlib
import re
import unittest

DOCKERFILE = (pathlib.Path(__file__).resolve().parents[1] /
              "infra/production/Gpoint.Dockerfile")


class DockerCacheV3Tests(unittest.TestCase):
    def test_backend_cache_not_invalidated_by_minapp_only_paths(self):
        lines = DOCKERFILE.read_text().splitlines()
        stages = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
        install = next(i for i, x in enumerate(stages) if x.startswith("RUN --mount=") and "pnpm install" in x)
        stable = next(i for i, x in enumerate(stages) if x == "COPY packages ./packages")
        backend = [i for i, x in enumerate(stages) if x.startswith("RUN pnpm --filter") and "build" in x and "@gpoint/miniapp" not in x]
        mini = next(i for i, x in enumerate(stages) if x == "COPY apps/miniapp ./apps/miniapp")
        client = next(i for i, x in enumerate(stages) if x == "RUN pnpm --filter @gpoint/miniapp build")
        full_context = [i for i, x in enumerate(stages) if x == "COPY . /app"]
        self.assertEqual(len(full_context), 1)
        self.assertLess(install, stable)
        self.assertTrue(backend)
        self.assertTrue(all(stable < i < mini for i in backend))
        self.assertLess(mini, client)
        self.assertLess(client, full_context[0])
        # SHA affects client-build output, never a cacheable backend layer.
        arg = next(i for i, x in enumerate(stages) if x == "ARG GPOINT_BUILD_SHA")
        self.assertTrue(all(i < arg for i in backend))
        self.assertLess(arg, client)
        self.assertIn("org.opencontainers.image.revision", DOCKERFILE.read_text() if "org.opencontainers.image.revision" in DOCKERFILE.read_text() else (pathlib.Path(__file__).resolve().parents[1] / "scripts/prepare-release.py").read_text())

    def test_skip_builder_teardown_only_on_disposable_hosted_prepare(self):
        workflow = (pathlib.Path(__file__).resolve().parents[1] /
                    ".github/workflows/gpoint-prepare.yml").read_text()
        self.assertIn("  prepare:", workflow)
        self.assertIn("  publish:", workflow)
        prepare = workflow.split("  prepare:", 1)[1].split("  publish:", 1)[0]
        # Not valid on a persistent self-hosted runner: cleanup there must
        # remain enabled to avoid leaked builders, resources, and build state.
        self.assertRegex(prepare, r"(?m)^    runs-on: ubuntu-latest\s*$")
        self.assertNotIn("self-hosted", prepare)
        builder = re.search(
            r"(?ms)^      - name: Set up isolated production-platform Buildx builder\n"
            r"(.*?)(?=^      - |\Z)", prepare)
        self.assertIsNotNone(builder)
        self.assertIn("docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069", builder.group(1))
        self.assertRegex(builder.group(1), r"(?m)^          platforms: linux/amd64$")
        self.assertRegex(builder.group(1), r"(?m)^          cleanup: false$")

    def test_source_graph_is_complete_and_keeps_runtime_stage(self):
        s = DOCKERFILE.read_text()
        for snippet in ("COPY tsconfig.base.json", "COPY apps/api ./apps/api",
                        "COPY apps/worker ./apps/worker", "COPY apps/bot ./apps/bot",
                        "COPY packages ./packages", "FROM build AS miniapp-build",
                        "COPY apps/miniapp ./apps/miniapp",
                        "COPY --from=build /workspace /app", "COPY . /app",
                        "COPY --from=miniapp-build /workspace/apps/miniapp/dist /app/apps/miniapp/dist"):
            self.assertIn(snippet, s)
        runtime = s.index("FROM node:22-bookworm-slim AS runtime")
        self.assertGreater(runtime, s.index("RUN pnpm --filter @gpoint/miniapp build"))
        self.assertLess(s.index("COPY --from=build /workspace /app"),
                        s.index("COPY . /app"))
        self.assertLess(s.index("COPY . /app"),
                        s.index("COPY --from=miniapp-build /workspace/apps/miniapp/dist /app/apps/miniapp/dist"))
        self.assertLess(s.index("RUN mkdir -p /app/apps/miniapp/node_modules/.vite-temp"),
                        s.index("COPY . /app"))
        # The final source overlay is small because the Docker context must
        # omit all generated dependency trees and built outputs.
        ignore = (pathlib.Path(__file__).resolve().parents[1] / ".dockerignore").read_text().splitlines()
        for excluded in ("node_modules", "**/node_modules", "dist", "**/dist"):
            self.assertIn(excluded, ignore)
        self.assertNotIn("COPY . .", [line.strip() for line in s.splitlines() if not line.lstrip().startswith("#")])



if __name__ == "__main__":
    unittest.main()
