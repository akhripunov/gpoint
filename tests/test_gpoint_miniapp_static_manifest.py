import importlib.util
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'gpoint-miniapp-static-manifest.py'
SPEC = importlib.util.spec_from_file_location('gpoint_miniapp_static_manifest', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
DOCKERFILE = Path(__file__).resolve().parents[1] / 'infra' / 'production' / 'Gpoint.Miniapp.Dockerfile'
WORKFLOW = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'gpoint-first-kamal-cutover.yml'


class MiniappStaticManifestTests(unittest.TestCase):
    def test_only_app_source_contracts_are_staged_as_miniapp_workspace_source(self):
        repository = DOCKERFILE.parents[2]
        dockerfile = DOCKERFILE.read_text(encoding='utf-8')
        imports = []
        for source in (repository / 'apps' / 'miniapp').rglob('*'):
            if source.suffix not in {'.ts', '.tsx', '.js', '.jsx'}:
                continue
            for specifier in re.findall(
                r'''(?:from\s*|import\s*\()\s*["']([^"']+)["']''',
                source.read_text(encoding='utf-8'),
            ):
                if not specifier.startswith('.'):
                    continue
                resolved = os.path.normpath(
                    os.path.join(source.parent.relative_to(repository), specifier)
                )
                if resolved.startswith('packages/'):
                    imports.append(resolved)

        package_sources = {path.split('/src/', 1)[0] + '/src' for path in imports}
        self.assertEqual(package_sources, {'packages/contracts/src'})
        self.assertIn(
            'COPY --from=app-source /packages/contracts/src ./packages/contracts/src',
            dockerfile,
        )
        copied_workspace_sources = re.findall(
            r'^COPY --from=app-source /(packages/[^ ]+) ', dockerfile, re.MULTILINE
        )
        self.assertEqual(copied_workspace_sources, ['packages/contracts/src'])

    def test_runtime_bases_are_immutable_and_nginx_digest_is_hosted_build_input(self):
        dockerfile = DOCKERFILE.read_text(encoding='utf-8')
        workflow = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn(
            'FROM docker.io/library/node:22-bookworm-slim@sha256:'
            'de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d',
            dockerfile,
        )
        self.assertIn('ARG NGINX_BASE_DIGEST', dockerfile)
        self.assertIn('FROM nginx:1.27-alpine@${NGINX_BASE_DIGEST}', dockerfile)
        self.assertIn('Resolve pinned Nginx linux/amd64 base digest', workflow)
        self.assertIn('nginx_base_digest: ${{ steps.nginx.outputs.digest }}', workflow)
        self.assertIn('NGINX_BASE_DIGEST=${{ steps.nginx.outputs.digest }}', workflow)
        self.assertIn('provenance: mode=max,version=v1', workflow)

    def test_production_timestamp_patch_is_validated_and_recorded(self):
        dockerfile = DOCKERFILE.read_text(encoding='utf-8')
        self.assertIn('ARG GPOINT_BUILD_TIME', dockerfile)
        self.assertIn('new Date(value).toISOString() !== value', dockerfile)
        self.assertIn("grep -Fc 'const buildTime = new Date().toISOString();'", dockerfile)
        self.assertIn('process.env.GPOINT_BUILD_TIME ?? new Date().toISOString()', dockerfile)
        self.assertIn('org.gpoint.source.patch="miniapp-build-time-from-production-metadata-v1"', dockerfile)

    def test_nginx_runtime_clears_inherited_default_pages_before_app_copy(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        nginx_stage = dockerfile.split("FROM nginx:1.27-alpine@", 1)[1]
        purge = "RUN rm -rf /usr/share/nginx/html/*"
        app_copy = "COPY --from=miniapp-build /workspace/apps/miniapp/dist/ /usr/share/nginx/html/"
        self.assertIn(purge, nginx_stage)
        self.assertIn(app_copy, nginx_stage)
        self.assertLess(nginx_stage.index(purge), nginx_stage.index(app_copy))
        self.assertNotIn("COPY --from=miniapp-build", nginx_stage.split(purge)[0])

    def test_same_static_tree_has_same_manifest_independent_of_root(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            copy = Path(temp) / 'copy'
            (source / 'assets').mkdir(parents=True)
            (source / 'index.html').write_bytes(b'<main id="root"></main>\n')
            (source / 'assets' / 'app.js').write_bytes(b'console.log("gpoint")\n')
            shutil.copytree(source, copy)
            self.assertEqual(MODULE.manifest(source), MODULE.manifest(copy))
            self.assertEqual(MODULE.manifest(source)['fileCount'], 2)

    def test_content_or_path_change_changes_the_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'index.html').write_bytes(b'first')
            before = MODULE.manifest(root)
            (root / 'index.html').write_bytes(b'second')
            self.assertNotEqual(before['sha256'], MODULE.manifest(root)['sha256'])

    def test_rejects_symlinks_and_empty_trees(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / 'empty'
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, 'empty'):
                MODULE.manifest(empty)
            tree = root / 'tree'
            tree.mkdir()
            (tree / 'index.html').write_text('ok', encoding='utf-8')
            (tree / 'link').symlink_to(tree / 'index.html')
            with self.assertRaisesRegex(ValueError, 'non-regular'):
                MODULE.manifest(tree)

    def test_option_rejects_symlink_in_live_path_ancestors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / 'real'
            (real / 'app' / 'miniapp').mkdir(parents=True)
            (real / 'app' / 'miniapp' / 'index.html').write_text('ok', encoding='utf-8')
            link = root / 'linked'
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink component'):
                MODULE.manifest(link / 'app' / 'miniapp', reject_symlink_ancestors=True)


if __name__ == '__main__':
    unittest.main()
