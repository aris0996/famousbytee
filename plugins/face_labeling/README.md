# Face Labeling Plugin

Optional class-scoped face detection and labeling for public and private gallery photos.

The plugin is disabled by default with `FACE_LABELING_ENABLED=0`. Install the
plugin dependencies from this directory only when the model and consent flow
have been tested:

```text
pip install -r plugins/face_labeling/requirements.txt
```

Required production settings when enabled:

- `FACE_LABELING_ENABLED=1`
- `FACE_EMBEDDING_ENCRYPTION_KEY=<Fernet key>`
- `FACE_LABELING_MODEL_DIR=<model cache directory>`

The public endpoint returns only normalized face boxes and quality scores. It
 never returns account IDs, names, usernames, or embeddings.
