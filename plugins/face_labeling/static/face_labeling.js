document.querySelectorAll('[data-face-photo]').forEach(async (photo) => {
    try {
        const response = await fetch(`/face-labeling/public/photos/${photo.dataset.facePhoto}`);
        if (!response.ok) return;
        const payload = await response.json();
        if (!payload.faces || !payload.faces.length) return;
        const layer = document.createElement('div');
        layer.className = 'public-face-layer';
        payload.faces.forEach((face) => {
            const box = document.createElement('span');
            box.className = 'public-face-box';
            const bbox = face.bbox || {};
            box.style.left = `${(bbox.left || 0) * 100}%`;
            box.style.top = `${(bbox.top || 0) * 100}%`;
            box.style.width = `${((bbox.right || 0) - (bbox.left || 0)) * 100}%`;
            box.style.height = `${((bbox.bottom || 0) - (bbox.top || 0)) * 100}%`;
            layer.appendChild(box);
        });
        photo.appendChild(layer);
    } catch (error) {
        // Public gallery remains usable when the optional plugin is unavailable.
    }
});
