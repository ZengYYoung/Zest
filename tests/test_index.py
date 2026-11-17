import json
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.index import RetrievalIndex
from zest.qa import prepare_context
from zest.storage import ProjectRegistry


class IndexTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self):
        (self.repo / "retry.md").write_text("payment retry policy\n订单超时重试策略\n")
        (self.repo / "other.md").write_text("database backup restore\n备份恢复\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def test_ranked_retrieval_and_provenance(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        metadata = index.build(p)
        result = index.search(p, "payment retry")
        self.assertEqual(result["matches"][0]["evidence"]["path"], "retry.md")
        self.assertGreater(result["matches"][0]["score"], 0)
        self.assertEqual(metadata["commit"], p["git"]["head_commit"])

    def test_chinese_bigram_query(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        self.assertEqual(index.search(p, "超时重试")["matches"][0]["evidence"]["path"], "retry.md")

    def test_persistence_and_no_matches(self):
        p = self.project()
        RetrievalIndex(self.state_dir).build(p)
        self.assertEqual(RetrievalIndex(self.state_dir).search(p, "unseen_word")["matches"], [])

    def test_stale_current_and_explicit_historical_index(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        old = p["git"]["head_commit"]
        (self.repo / "retry.md").write_text("new version")
        self.commit("new")
        with self.assertRaises(FileReadError) as exc:
            index.search(p, "retry")
        self.assertEqual(exc.exception.code, "index_stale")
        self.assertTrue(index.search(p, "retry", commit=old)["matches"])

    def test_uncommitted_changes_excluded(self):
        p = self.project()
        (self.repo / "retry.md").write_text("secret_new_word")
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        self.assertEqual(index.search(p, "secret_new_word")["matches"], [])

    def test_sensitive_file_skipped(self):
        p = self.project()
        (self.repo / "token.py").write_text("ghp_" + "a" * 30)
        self.commit("sensitive fixture")
        index = RetrievalIndex(self.state_dir)
        metadata = index.build(p)
        self.assertEqual(metadata["skipped_counts"]["sensitive_content"], 1)

    def test_chunk_line_boundaries(self):
        p = self.project()
        (self.repo / "retry.md").write_text("retry\n" * 80)
        self.commit("long doc")
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        result = index.search(p, "retry", limit=20)
        ranges = {(m["evidence"]["start_line"], m["evidence"]["end_line"]) for m in result["matches"]}
        self.assertIn((1, 40), ranges)
        self.assertIn((38, 77), ranges)

    def test_build_budget_does_not_replace_existing_index(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        with patch("zest.index.MAX_INDEX_FILES", 1), self.assertRaises(FileReadError):
            index.build(p)
        self.assertTrue(index.search(p, "retry")["matches"])

    def test_missing_and_corrupt_index_errors(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        with self.assertRaises(FileReadError):
            index.search(p, "retry")
        index.db_path.write_bytes(b"not sqlite")
        with self.assertRaises(FileReadError):
            index.search(p, "retry")

    def test_qa_lexical_sources_are_reread_from_git(self):
        p = self.project()
        RetrievalIndex(self.state_dir).build(p)
        context = prepare_context(p, "payment retry", state_dir=self.state_dir, retrieval="lexical")
        self.assertEqual(context["sources"][0]["evidence"]["path"], "retry.md")
        self.assertIn("payment", context["sources"][0]["lines"][0]["text"])

    def test_cli_build_search(self):
        p = self.project()
        code, stdout, stderr = self.cli("index", "build", p["id"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["backend"], "lexical_tfidf")
        code, stdout, stderr = self.cli("index", "search", p["id"], "retry")
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["matches"])

    def test_index_record_mismatch_is_rejected(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        with closing(sqlite3.connect(index.db_path)) as conn, conn:
            conn.execute("UPDATE indexes SET chunks='[]'")
        with self.assertRaises(FileReadError) as exc:
            index.search(p, "retry")
        self.assertEqual(exc.exception.code, "index_corrupted")

    def test_time_budget_checked_inside_directory(self):
        p = self.project()
        with patch("zest.index.time.monotonic", side_effect=[0, 0, 31]):
            with self.assertRaises(FileReadError) as exc:
                RetrievalIndex(self.state_dir).build(p)
        self.assertEqual(exc.exception.code, "index_budget_exceeded")

    def test_diff_reuses_unchanged_blob_and_updates_complete_snapshot(self):
        from zest.files import load_text_blob
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        base = index.build(p)['commit']
        (self.repo / 'retry.md').write_text('new payment processing\n')
        (self.repo / 'added.py').write_text('def fresh():\n    return 42\n')
        target = self.commit('modify and add')
        reads = []
        def read(root, entry):
            reads.append(entry.path)
            return load_text_blob(root, entry)
        with patch('zest.index.load_text_blob', side_effect=read):
            metadata = index.build(p)
        self.assertEqual(metadata['diff_base_commit'], base)
        self.assertNotIn('other.md', reads)
        self.assertIn('retry.md', reads)
        self.assertIn('added.py', reads)
        self.assertGreaterEqual(metadata['files_diff_reused'], 1)
        result = index.search(p, 'database backup')
        self.assertEqual(result['matches'][0]['evidence']['commit'], target)
        self.assertEqual(result['matches'][0]['evidence']['path'], 'other.md')

    def test_diff_handles_deletion_rename_and_old_version(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        old = index.build(p)['commit']
        (self.repo / 'other.md').unlink()
        (self.repo / 'retry.md').rename(self.repo / 'renamed.md')
        new = self.commit('rename and delete')
        index.build(p)
        self.assertEqual(index.search(p, 'payment retry')['matches'][0]['evidence']['path'], 'renamed.md')
        self.assertEqual(index.search(p, 'database backup')['matches'], [])
        self.assertEqual(index.search(p, 'database backup', commit=old)['matches'][0]['evidence']['path'], 'other.md')
        self.assertEqual(index.search(p, 'payment retry')['matches'][0]['evidence']['commit'], new)

    def test_diff_failure_and_invalid_manifest_fall_back_to_verified_scan(self):
        p = self.project()
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        self.git('commit', '--allow-empty', '-m', 'empty')
        with patch('zest.index.compare_commits', side_effect=ValueError('invalid diff')):
            result = index.build(p)
        self.assertIsNone(result['diff_base_commit'])
        self.assertEqual(result['files_diff_reused'], 0)
        self.assertTrue(index.search(p, 'payment retry')['matches'])

        with closing(sqlite3.connect(index.db_path)) as conn, conn:
            row = conn.execute('SELECT metadata FROM indexes WHERE commit_id=?', (result['commit'],)).fetchone()
            metadata = json.loads(row[0])
            metadata['indexed_files']['retry.md']['bytes'] = 0
            conn.execute('UPDATE indexes SET metadata=? WHERE commit_id=?', (json.dumps(metadata), result['commit']))
        fallback = index.build(p)
        self.assertIsNone(fallback['diff_base_commit'])
        self.assertEqual(fallback['files_diff_reused'], 0)
        self.assertTrue(index.search(p, 'payment retry')['matches'])


if __name__ == "__main__":
    unittest.main()
