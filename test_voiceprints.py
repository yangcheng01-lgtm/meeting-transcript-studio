from __future__ import annotations

import unittest

import voiceprints


class VoiceprintTests(unittest.TestCase):
    def test_cosine_identical(self) -> None:
        self.assertAlmostEqual(voiceprints.cosine_similarity([1, 0], [1, 0]), 1.0)

    def test_cosine_orthogonal(self) -> None:
        self.assertAlmostEqual(voiceprints.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_cosine_empty(self) -> None:
        self.assertEqual(voiceprints.cosine_similarity([], [1, 2]), 0.0)

    def _person(self, vector: list[float], name: str = "John") -> dict:
        return {"id": "p1", "name": name, "role": "tech", "voiceprints": [{"vector": vector}]}

    def test_auto_match(self) -> None:
        result = voiceprints.match_speaker([1.0, 0.0], [self._person([1.0, 0.0])])
        self.assertEqual(result["decision"], "auto")
        self.assertEqual(result["name"], "John")

    def test_review_match(self) -> None:
        result = voiceprints.match_speaker([1.0, 0.0], [self._person([0.7, 0.714])])
        self.assertEqual(result["decision"], "review")

    def test_new_when_no_match(self) -> None:
        result = voiceprints.match_speaker([1.0, 0.0], [self._person([0.0, 1.0])])
        self.assertEqual(result["decision"], "new")

    def test_match_best_across_people(self) -> None:
        people = [self._person([0.0, 1.0], "A"), self._person([1.0, 0.1], "B")]
        result = voiceprints.match_speaker([1.0, 0.0], people)
        self.assertEqual(result["name"], "B")

    def test_add_and_ensure_voiceprint(self) -> None:
        payload = {"customers": [{"id": "c1", "name": "Voi", "people": []}]}
        pid = voiceprints.ensure_person_with_voiceprint(payload, "c1", "John", "tech", [1.0, 0.0])
        self.assertTrue(voiceprints.add_voiceprint(payload, "c1", pid, [0.0, 1.0]))
        person = payload["customers"][0]["people"][0]
        self.assertEqual(len(person["voiceprints"]), 2)


if __name__ == "__main__":
    unittest.main()