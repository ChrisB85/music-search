import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "music_search"))
from music_search import Index, Service, norm, spoken_name  # noqa: E402


def artist(name, n):
    return {"name": name, "uri": f"library://artist/{n}"}


def item(kind, name, by, n):
    return {"name": name, "uri": f"library://{kind}/{n}", "artists": [{"name": by}]}


LIBRARY = {
    "artist": [artist("Marilyn Manson", 1), artist("Black Sabbath", 2), artist("Type O Negative", 3),
               artist("Iron Maiden", 4), artist("Metallica", 5)],
    "album": [item("album", "Paranoid", "Black Sabbath", 1), item("album", "Antichrist Superstar", "Marilyn Manson", 2)],
    "track": [item("track", "Paranoid", "Black Sabbath", 1), item("track", "Paranoid", "Black Sabbath", 2),
              item("track", "Paranoid", "Type O Negative", 3), item("track", "I", "Black Sabbath", 4),
              item("track", "Enter Sandman", "Metallica", 5)],
}
ALIASES = {"Iron Maiden": ["Ajron Mejden"]}


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.ix = Index(LIBRARY, ALIASES)

    def best(self, *args):
        return self.ix.search(*args)[0]

    def test_norm(self):
        self.assertEqual(norm("  Motörhead & Łódź! "), "motorhead lodz")

    def test_spoken_name_strips_command(self):
        self.assertEqual(spoken_name("Puść Judas Priest w pokoju."), "Judas Priest")
        self.assertEqual(spoken_name("Metalikę"), "Metalikę")
        self.assertEqual(spoken_name("włącz Dżudas Prista"), "Dżudas Prista")

    def test_inflected_artist(self):
        self.assertEqual(self.best("Marlina Mansona")["name"], "Marilyn Manson")

    def test_phonetic_spelling(self):
        self.assertEqual(self.best("Metalika", "", "artist")["name"], "Metallica")

    def test_alias(self):
        hit = self.best("Ajron Mejden")
        self.assertEqual((hit["name"], hit["score"]), ("Iron Maiden", 100.0))

    def test_title_with_misheard_artist(self):
        hit = self.best("Paranoid", "Black Sabat", "track")
        self.assertEqual((hit["name"], hit["artist"]), ("Paranoid", "Black Sabbath"))

    def test_one_string_query(self):
        hit = self.best("Paranoid Black Sabbath")
        self.assertEqual((hit["name"], hit["artist"]), ("Paranoid", "Black Sabbath"))

    def test_bare_artist_beats_their_tracks(self):
        self.assertEqual(self.best("Merlin Manson")["type"], "artist")

    def test_short_name_inside_unknown_artist(self):
        # Regression: WRatio gave "Zenek Martyniuk" 75 against "Martyr" and played it.
        ix = Index({"artist": [artist("Martyr", 9)]}, {})
        self.assertLess(ix.search("Zenek Martyniuk", "", "artist")[0]["score"], 75)

    def test_duplicate_tracks_collapsed(self):
        hits = self.ix.search("Paranoid", "Black Sabbath", "track", 5)
        self.assertEqual(sum(h["name"] == "Paranoid" for h in hits), 1)


class FakeService(Service):
    STATES = [
        {"entity_id": "person.krzysiek", "attributes": {"friendly_name": "Krzysiek"}},
        {"entity_id": "person.aurelia", "attributes": {"friendly_name": "Aurelia"}},
        {"entity_id": "sensor.asystent_krzysiek", "attributes": {"person": "person.krzysiek", "conversation_engine": "conversation.alexa"}},
        {"entity_id": "sensor.asystent_aurelia", "attributes": {"person": "person.aurelia", "conversation_engine": "conversation.nabu"}},
        {"entity_id": "sensor.asystent_domyslny", "attributes": {"conversation_engine": "conversation.nabu"}},
    ]

    def ha(self, method, path, body=None):
        return self.STATES


class UserTest(unittest.TestCase):
    def setUp(self):
        self.svc = FakeService({"users": [{"person": "person.krzysiek", "ma_user": "krzysztof"},
                                          {"person": "person.aurelia", "ma_user": "aurelia"}]})

    def test_agent_maps_to_person(self):
        self.assertEqual(self.svc.resolve_user("conversation.alexa"), ("krzysztof", "person.krzysiek"))
        self.assertEqual(self.svc.resolve_user("conversation.nabu"), ("aurelia", "person.aurelia"))

    def test_named_library_wins_over_agent(self):
        self.assertEqual(self.svc.resolve_user("conversation.alexa", "Aurelii"), ("aurelia", "person.aurelia"))
        self.assertEqual(self.svc.resolve_user("", "Krzyśka"), ("krzysztof", "person.krzysiek"))

    def test_unknown_means_all_libraries(self):
        self.assertEqual(self.svc.resolve_user("conversation.other"), ("", ""))
        self.assertEqual(self.svc.resolve_user("", "Zbyszek"), ("", ""))


class AliasEditTest(unittest.TestCase):
    def setUp(self):
        self.svc = FakeService({})
        self.svc.aliases_file = os.path.join(tempfile.mkdtemp(), "aliases.yaml")
        self.svc.libraries = {"": LIBRARY}
        self.svc.rebuild()

    def test_add_alias_with_approximate_artist(self):
        result = self.svc.add_alias("Black Sabat", "Blek Sabat")
        self.assertEqual(result["artist"], "Black Sabbath")
        self.assertEqual(self.svc.index.search("Blek Sabat", "", "artist")[0]["score"], 100.0)

    def test_alias_of_another_artist_rejected(self):
        self.assertIn("error", self.svc.set_aliases("Metallica", ["Iron Maiden"]))

    def test_unknown_artist_rejected(self):
        self.assertIn("error", self.svc.add_alias("Zenek Martyniuk", "Zenek"))

    def test_duplicate_alias_not_added_twice(self):
        self.svc.add_alias("Iron Maiden", "Ajron Mejden")
        self.svc.add_alias("Iron Maiden", "ajron mejden")
        self.assertEqual(self.svc.load_aliases()["Iron Maiden"], ["Ajron Mejden"])


class MissesTest(unittest.TestCase):
    def test_delete_miss(self):
        svc = FakeService({})
        svc.misses_file = os.path.join(tempfile.mkdtemp(), "misses.log")
        with open(svc.misses_file, "w", encoding="utf-8") as f:
            f.write("a\tq=x\nb\tq=y\n")
        svc.delete_miss("a\tq=x")
        self.assertEqual(svc.misses(), ["b\tq=y"])
        self.assertIn("error", svc.delete_miss("zzz"))


if __name__ == "__main__":
    unittest.main()
