"""Saved comparisons and the names a comparison gives its runs.

Run: python -m unittest tests.test_saved_compares
"""
import tempfile
import unittest
from pathlib import Path

from microwave_package import paths, webapp
from microwave_package.workflow import saved_compares
from microwave_package.workflow.run_manager import Run

from tests.test_compare_runs import WITH_BONDS, WITHOUT_BONDS, RunsOnDisk


class SavedComparesTests(RunsOnDisk):
    def setUp(self):
        super().setUp()
        saved = paths.SAVED_COMPARES_DIR
        paths.SAVED_COMPARES_DIR = Path(tempfile.mkdtemp(prefix="saved_"))
        self.addCleanup(setattr, paths, "SAVED_COMPARES_DIR", saved)
        self.client = webapp.app.test_client()

    def test_save_list_rename_delete(self):
        record = saved_compares.save("bonds", {"runs": [WITH_BONDS], "names": {WITH_BONDS: " mine "},
                                               "options": {"x": "freq_ghz"}})
        self.assertEqual(saved_compares.get(record["id"])["names"], {WITH_BONDS: "mine"})
        self.assertEqual([r["name"] for r in saved_compares.listing()], ["bonds"])
        saved_compares.rename(record["id"], "renamed")
        self.assertEqual(saved_compares.get(record["id"])["name"], "renamed")
        self.assertTrue(saved_compares.delete(record["id"]))
        self.assertEqual(saved_compares.listing(), [])

    def test_a_name_is_required_and_ids_are_not_paths(self):
        with self.assertRaises(ValueError):
            saved_compares.save("  ", {"runs": []})
        self.assertIsNone(saved_compares.get("../../etc/passwd"))
        self.assertFalse(saved_compares.delete("../x"))

    def test_names_apply_to_the_comparison_only(self):
        self.wire_bond_pair()
        url = f"runs={WITH_BONDS},{WITHOUT_BONDS}&n_{WITH_BONDS}=Alpha"
        html = self.client.get(f"/compare/plot?{url}&x=mode&y=freq_ghz").get_data(as_text=True)
        self.assertIn("Alpha", html)
        self.assertNotIn("with bonds", html)
        self.assertIn("without bonds", html)
        csv = self.client.get(f"/compare/results.csv?{url}").get_data(as_text=True)
        self.assertIn("A · Alpha", csv)
        self.assertEqual(Run.load(WITH_BONDS).label, "with bonds")

    def test_a_saved_compare_reopens_with_its_runs_and_names_and_via_the_routes(self):
        self.wire_bond_pair()
        reply = self.client.post("/compare/saved", json={
            "name": "pair", "runs": [WITH_BONDS, WITHOUT_BONDS], "names": {WITHOUT_BONDS: "Beta"}})
        saved_id = reply.get_json()["id"]
        page = self.client.get(f"/compare?saved={saved_id}").get_data(as_text=True)
        self.assertIn("Beta", page)
        self.assertIn(WITH_BONDS, page)
        self.assertEqual(self.client.post(f"/compare/saved/{saved_id}/rename", json={"name": "two"}).status_code, 200)
        self.assertEqual(self.client.post(f"/compare/saved/{saved_id}/delete").status_code, 200)
        self.assertEqual(self.client.post(f"/compare/saved/{saved_id}/delete").status_code, 404)
        self.assertEqual(self.client.post("/compare/saved", json={"name": ""}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
