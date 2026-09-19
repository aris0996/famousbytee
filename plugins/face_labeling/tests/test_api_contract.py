import unittest
from datetime import datetime
from types import SimpleNamespace

from plugins.face_labeling.api import _bbox, _photo_payload


class FaceApiContractTests(unittest.TestCase):
    def test_bbox_is_normalized_and_clamped(self):
        face = SimpleNamespace(
            bbox_json='{"left": -0.2, "top": 0.25, "right": 1.4, "bottom": 0.9}'
        )
        self.assertEqual(
            _bbox(face),
            {'left': 0.0, 'top': 0.25, 'right': 1.0, 'bottom': 0.9},
        )

    def test_invalid_bbox_is_hidden(self):
        self.assertIsNone(_bbox(SimpleNamespace(bbox_json='not-json')))

    def test_mobile_payload_does_not_contain_identity_embedding(self):
        photo = SimpleNamespace(
            id=7,
            filename='photo.jpg',
            thumbnail='thumb.jpg',
            caption='Kegiatan kelas',
            tags='#kelas',
            status='Published',
            is_public=True,
            classroom_id=2,
            user=SimpleNamespace(full_name='Uploader'),
            created_at=datetime(2026, 9, 19),
        )
        payload = _photo_payload(
            photo,
            face_data={
                'enabled': True,
                'status': 'completed',
                'face_count': 1,
                'faces': [{'bbox': {'left': 0, 'top': 0, 'right': 1, 'bottom': 1}}],
            },
        )
        self.assertEqual(payload['face']['face_count'], 1)
        self.assertNotIn('embedding', payload)
        self.assertNotIn('embedding_ciphertext', payload)


if __name__ == '__main__':
    unittest.main()
