from .config import MODEL_NAME, model_root


class FaceEngineUnavailable(RuntimeError):
    pass


class InsightFaceEngine:
    def __init__(self):
        try:
            import cv2
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise FaceEngineUnavailable(
                'Engine wajah belum terpasang. Instal requirements plugin face_labeling.'
            ) from exc

        root = model_root()
        self._cv2 = cv2
        try:
            options = {'name': MODEL_NAME, 'providers': ['CPUExecutionProvider']}
            if root:
                options['root'] = root
            self._app = FaceAnalysis(**options)
            self._app.prepare(ctx_id=-1, det_size=(640, 640))
        except Exception as exc:
            raise FaceEngineUnavailable(f'Model wajah gagal dimuat: {exc}') from exc

    def detect(self, image_path):
        image = self._cv2.imread(image_path)
        if image is None:
            raise FaceEngineUnavailable('File foto tidak dapat dibaca.')
        faces = self._app.get(image)
        height, width = image.shape[:2]
        results = []
        for face in faces:
            bbox = [float(value) for value in face.bbox.tolist()]
            embedding = getattr(face, 'normed_embedding', None)
            if embedding is None:
                embedding = face.embedding
            quality = float(getattr(face, 'det_score', 0) or 0)
            results.append({
                'bbox': {
                    'left': max(0, min(1, bbox[0] / width)),
                    'top': max(0, min(1, bbox[1] / height)),
                    'right': max(0, min(1, bbox[2] / width)),
                    'bottom': max(0, min(1, bbox[3] / height)),
                },
                'embedding': [float(value) for value in embedding.tolist()],
                'quality_score': quality,
            })
        return results


def get_engine():
    return InsightFaceEngine()
