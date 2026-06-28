# Multiplayer Game Ideas

## Goal
Tambah game multiplayer ringan berbasis websocket yang cocok untuk landing page dan tidak membebani aplikasi utama.

## Concept Candidates

### 1. Battle Quiz Arena
- Pemain masuk ke arena yang sama.
- Pertanyaan muncul real-time.
- Jawaban cepat memberi damage ke lawan.
- Skor dan health update langsung via websocket.

### 2. Solo War Arena
- Tidak berbasis kelompok.
- Tiap pemain punya unit sendiri.
- Pemain mengumpulkan resource, menyerang, dan bertahan secara real-time.
- Cocok untuk mode casual dan kompetitif.

### 3. Zone Control
- Arena dibagi beberapa titik.
- Siapa yang bertahan paling lama mendapatkan poin.
- Gameplay sederhana dan cepat dipahami.

### 4. Rapid Clash
- Pemain saling adu skill/click timing.
- Serangan tidak berbasis tim, murni free-for-all.
- Cocok untuk sesi singkat di landing page.

## Recommended First Build
- Battle Quiz Arena jika ingin interaksi yang paling aman dan paling dekat dengan gaya portal edukasi.
- Solo War Arena jika ingin kesan lebih seru dan kompetitif tanpa membuat pemain merasa masuk tim tertentu.

## Technical Notes
- Gunakan websocket untuk state real-time.
- Simpan state match di server, jangan di client.
- Buat room kecil agar ringan.
- Sediakan fallback polling jika websocket gagal.
- Pastikan game tidak mengganggu performa landing page.
