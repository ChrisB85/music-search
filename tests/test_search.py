import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "music_search"))
from music_search import Index, norm  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
