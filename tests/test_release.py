"""scripts/release.sh, driven against throwaway repositories.

The script's whole job is to rewrite pins nobody looks at, in files that are
run by eight other repositories. Asserting about its text would prove
nothing: what matters is which lines come out changed, and -- more than that
-- which lines come out UNCHANGED.

Every case here is a way the rewrite could be wrong while still looking
right: a ref belonging to another repository, a version that already exists,
a source that is not main, and the silent one, a pattern that stops matching
and repins nothing at all.
"""
import re
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "scripts/release.sh"

# These tests are about THIS repository, and helpers/ and tests/ are vendored
# wholesale into every product. There, ROOT is the product"s
# scripts/engineering directory: scripts/release.sh does not exist, and the
# workflows on disk are the product"s own, which have no cicd self-checkouts
# to agree about. Running them in a product asserts things that are not its
# business and fails for reasons it cannot fix.
#
# SOURCE.json beside this tests/ directory is the signal: it is written by
# vendor-snapshot.py and exists only in a consumer, never here.
VENDORED = (ROOT / "SOURCE.json").exists()
SHA = "b" * 40


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@unittest.skipIf(VENDORED, "cicd-only: this repository's release script")
class ReleasePrepare(unittest.TestCase):
    def build(self, workflow):
        """An origin plus a clone of it, with one workflow file."""
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)

        origin = base / "origin"
        origin.mkdir()
        git(origin, "init", "--quiet", "--initial-branch=main")
        git(origin, "config", "user.email", "t@t.invalid")
        git(origin, "config", "user.name", "t")
        (origin / ".github/workflows").mkdir(parents=True)
        (origin / ".github/workflows/backup.yml").write_text(workflow)
        (origin / "scripts").mkdir()
        (origin / "scripts/release.sh").write_text(RELEASE.read_text())
        git(origin, "add", "-A")
        git(origin, "commit", "--quiet", "-m", "base")

        clone = base / "clone"
        git(base, "clone", "--quiet", str(origin), str(clone))
        git(clone, "config", "user.email", "t@t.invalid")
        git(clone, "config", "user.name", "t")
        return clone, git(clone, "rev-parse", "origin/main").stdout.strip()

    def run_prepare(self, clone, version, sha):
        return subprocess.run(
            ["sh", "scripts/release.sh", "prepare", version, sha],
            cwd=clone, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def workflow(self, own_ref=SHA, extra=""):
        return textwrap.dedent(f"""\
            name: Backup
            on: {{workflow_call: {{}}}}
            jobs:
              run:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/checkout@{"a" * 40}
                    with:
                      repository: nicodes/cicd
                      ref: {own_ref} # helpers (bump-along rule)
                      path: .cicd
            {extra}""")

    # --- the thing it is for -------------------------------------------
    def test_self_checkout_is_repinned_to_the_source_commit(self):
        clone, source = self.build(self.workflow())
        result = self.run_prepare(clone, "v0.1.0", source)
        self.assertEqual(result.returncode, 0, result.stdout)
        body = git(clone, "show", f"release/v0.1.0:.github/workflows/backup.yml").stdout
        self.assertIn(f"ref: {source}", body)
        self.assertNotIn(SHA, body)
        # The trailing comment is the operator's note about WHY that pin
        # exists. Rewriting the sha must not eat it.
        self.assertIn("# helpers (bump-along rule)", body)

    def test_another_repositorys_ref_is_left_alone(self):
        # The pattern keys on `repository: nicodes/cicd`. A pinned checkout of
        # something else must survive untouched -- rewriting it to a cicd
        # commit would be wrong and almost invisible.
        other = textwrap.dedent(f"""\
              other:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/checkout@{"a" * 40}
                    with:
                      repository: someone/else
                      ref: {"c" * 40}
            """)
        clone, source = self.build(self.workflow(extra=other))
        result = self.run_prepare(clone, "v0.1.0", source)
        self.assertEqual(result.returncode, 0, result.stdout)
        body = git(clone, "show", "release/v0.1.0:.github/workflows/backup.yml").stdout
        self.assertIn(f"ref: {'c' * 40}", body)
        self.assertIn(f"ref: {source}", body)

    def test_renamed_self_checkout_is_repinned(self):
        clone, source = self.build(self.workflow().replace('repository: nicodes/cicd', 'repository: nicodes/tools'))
        result = self.run_prepare(clone, 'v0.9.1', source)
        self.assertEqual(result.returncode, 0, result.stdout)
        body = git(clone, 'show', 'release/v0.9.1:.github/workflows/backup.yml').stdout
        self.assertIn('repository: nicodes/tools', body)
        self.assertIn(f'ref: {source}', body)
        self.assertNotIn(SHA, body)

    def test_repinning_nothing_is_a_failure(self):
        # The silent one. If the self-checkouts are gone this script has
        # outlived its purpose; if the pattern stopped matching it would
        # otherwise ship a release claiming a pin it never wrote.
        bare = "name: Backup\non: {workflow_call: {}}\njobs: {}\n"
        clone, source = self.build(bare)
        result = self.run_prepare(clone, "v0.1.0", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no helper checkout pins were rewritten", result.stdout)

    # --- inputs must be exact ------------------------------------------
    def test_a_source_that_is_not_main_is_refused(self):
        clone, source = self.build(self.workflow())
        result = self.run_prepare(clone, "v0.1.0", "d" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not the tip of origin/main", result.stdout)

    def test_an_existing_version_is_refused(self):
        clone, source = self.build(self.workflow())
        git(clone, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        git(clone, "push", "--quiet", "origin", "v0.1.0")
        result = self.run_prepare(clone, "v0.1.0", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stdout)

    def test_malformed_inputs_are_refused(self):
        clone, source = self.build(self.workflow())
        for version, sha in [("0.1.0", source), ("v0.1", source),
                             ("v0.1.0", "short"), ("v0.1.0", "z" * 40)]:
            with self.subTest(version=version, sha=sha):
                self.assertNotEqual(self.run_prepare(clone, version, sha).returncode, 0)

    # --- reproducibility ------------------------------------------------
    def test_the_same_inputs_produce_the_same_tree(self):
        clone, source = self.build(self.workflow())
        first = self.run_prepare(clone, "v0.1.0", source)
        self.assertEqual(first.returncode, 0, first.stdout)
        tree = re.search(r"tree ([0-9a-f]{40})", first.stdout).group(1)
        git(clone, "branch", "-D", "release/v0.1.0")
        second = self.run_prepare(clone, "v0.1.0", source)
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(re.search(r"tree ([0-9a-f]{40})", second.stdout).group(1), tree)

    def test_it_pushes_nothing_without_push(self):
        clone, source = self.build(self.workflow())
        self.assertEqual(self.run_prepare(clone, "v0.1.0", source).returncode, 0)
        remote = git(clone, "ls-remote", "--heads", "origin", "release/v0.1.0").stdout
        self.assertEqual(remote.strip(), "", "prepare pushed without being asked to")


if __name__ == "__main__":
    unittest.main()
