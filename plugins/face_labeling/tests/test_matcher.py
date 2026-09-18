import unittest

from plugins.face_labeling.matcher import cosine_similarity


class MatcherTests(unittest.TestCase):
    def test_identical_embeddings_score_one(self):
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal_embeddings_score_zero(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_invalid_embeddings_are_not_a_match(self):
        self.assertEqual(cosine_similarity([], [1]), 0.0)
        self.assertEqual(cosine_similarity([1], [1, 0]), 0.0)


if __name__ == '__main__':
    unittest.main()
