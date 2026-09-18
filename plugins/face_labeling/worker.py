"""Isolated worker for native face detection outside the Apache process."""

import argparse

from app import app

from .jobs import process_photo


def main():
    parser = argparse.ArgumentParser(description='Process one face-labeling photo.')
    parser.add_argument('--photo-id', type=int, required=True)
    args = parser.parse_args()

    with app.app_context():
        result = process_photo(args.photo_id)
        app.logger.info(
            'Face worker finished for photo %s: %s',
            args.photo_id,
            result,
        )
        return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
