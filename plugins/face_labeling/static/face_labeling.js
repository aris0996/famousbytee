document.querySelectorAll('[data-face-photo]').forEach(async (photo) => {
    try {
        const response = await fetch(`/face-labeling/public/photos/${photo.dataset.facePhoto}`);
        if (!response.ok) return;
        const payload = await response.json();
        if (payload.face_count == null) return;
        const badge = document.createElement('span');
        badge.className = 'public-face-count';
        badge.textContent = `${payload.face_count} wajah terdeteksi`;
        photo.appendChild(badge);
    } catch (error) {
        // Public gallery remains usable when the optional plugin is unavailable.
    }
});
