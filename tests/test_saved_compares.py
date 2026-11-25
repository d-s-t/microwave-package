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

    def test_every_held_parameter_is_an_option_and_the_shape_can_be_chosen(self):
        self.wire_bond_pair()
        runs = f"{WITH_BONDS},{WITHOUT_BONDS}"
        page = self.client.get(f"/compare?runs={runs}").get_data(as_text=True)
        self.assertIn('id="axis-symbol"', page)
        axes = page.split('id="axis-x"')[1].split("</select>")[0]
        self.assertIn('value="pcb_length"', axes)        # held by both runs, never swept
        self.assertIn('value="bonding_clearence"', axes)
        plot = self.client.get(f"/compare/plot?runs={runs}&x=pcb_length&y=freq_ghz&color=bonding_clearence&symbol=bonding_clearence")
        self.assertEqual(plot.status_code, 200)
        html = plot.get_data(as_text=True)
        self.assertIn('"itemclick":false', html.replace(" ", ""))  # a run cannot be hidden from the legend
        for symbol in ("", "run", "nonsense"):
            self.assertEqual(self.client.get(f"/compare/plot?runs={runs}&x=mode&y=freq_ghz&symbol={symbol}").status_code, 200)

    def test_the_runs_of_a_saved_compare_can_be_changed_and_the_change_saved(self):
        self.wire_bond_pair()
        saved_id = self.client.post("/compare/saved", json={
            "name": "pair", "runs": [WITH_BONDS, WITHOUT_BONDS], "names": {WITHOUT_BONDS: "Beta"}}).get_json()["id"]
        # the run list comes back ticked with the saved runs, and keeps the id for the new selection
        index = self.client.get(f"/?compare={WITH_BONDS},{WITHOUT_BONDS}&saved={saved_id}").get_data(as_text=True)
        self.assertIn(f'name="saved" value="{saved_id}"', index)
        self.assertIn("change runs", index)
        # the comparison with one run taken out is still that saved comparison (so it can be updated),
        # and the runs that stay keep the names it gave them
        page = self.client.get(f"/compare?saved={saved_id}&runs={WITHOUT_BONDS}").get_data(as_text=True)
        self.assertIn("update “pair”", page)
        self.assertIn("Beta", page)
        self.assertNotIn(f'data-run-row="{WITH_BONDS}"', page)
        self.assertIn(f"saved={saved_id}", page.split("Change which runs")[0].rsplit("href=", 1)[1])
        # updating stores the new set of runs under the same id
        self.client.post("/compare/saved", json={"id": saved_id, "name": "pair", "runs": [WITHOUT_BONDS]})
        self.assertEqual(saved_compares.get(saved_id)["runs"], [WITHOUT_BONDS])

    def test_the_order_sets_the_letters_and_is_saved(self):
        self.wire_bond_pair()
        with webapp.app.test_request_context(
                f"/compare?runs={WITH_BONDS},{WITHOUT_BONDS}&order={WITHOUT_BONDS},{WITH_BONDS}"):
            entries, _ = webapp._compared_runs(webapp.request.args)
        self.assertEqual([(e.run_id, e.letter) for e in entries],
                         [(WITHOUT_BONDS, "A"), (WITH_BONDS, "B")])
        saved_id = self.client.post("/compare/saved", json={
            "name": "flipped", "runs": [WITHOUT_BONDS, WITH_BONDS]}).get_json()["id"]
        page = self.client.get(f"/compare?saved={saved_id}").get_data(as_text=True)
        self.assertLess(page.index(f'data-run-row="{WITHOUT_BONDS}"'), page.index(f'data-run-row="{WITH_BONDS}"'))

    def test_runs_are_tagged_with_their_saved_compares_and_it_follows_rename_and_delete(self):
        self.wire_bond_pair()
        first = saved_compares.save("alpha cmp", {"runs": [WITH_BONDS, WITHOUT_BONDS]})
        for name in ("beta cmp", "gamma cmp"):
            saved_compares.save(name, {"runs": [WITH_BONDS]})
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"/compare?saved={first['id']}", index)
        self.assertIn("alpha cmp", index)
        self.assertIn("2 runs", index)
        self.assertEqual([t["name"] for t in saved_compares.tags_by_run()[WITH_BONDS]],
                         ["alpha cmp", "beta cmp", "gamma cmp"])
        self.assertEqual([t["name"] for t in saved_compares.tags_by_run()[WITHOUT_BONDS]], ["alpha cmp"])
        page = self.client.get(f"/runs/{WITH_BONDS}").get_data(as_text=True)
        self.assertIn("alpha cmp", page)
        self.assertIn(">+1<", page)  # three compares: two named, one folded
        saved_compares.rename(first["id"], "renamed cmp")
        self.assertIn("renamed cmp", self.client.get(f"/runs/{WITHOUT_BONDS}").get_data(as_text=True))
        saved_compares.delete(first["id"])
        self.assertNotIn(WITHOUT_BONDS, saved_compares.tags_by_run())


if __name__ == "__main__":
    unittest.main()
