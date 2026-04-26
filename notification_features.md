# 🔔 Sistem Notifikasi Famousbytee.b

Dokumen ini merangkum status saat ini dari sistem notifikasi dan rencana pengembangan fitur di masa depan.

## ✅ Fitur yang Sudah Ada saat Ini
1.  **Push Notifications (FCM)**: Pengiriman pesan real-time ke perangkat Android menggunakan Firebase Cloud Messaging.
2.  **Broadcast Messaging**: Admin dapat mengirim pengumuman ke seluruh anggota kelas sekaligus.
3.  **Targeted Messaging**: Admin dapat memilih anggota spesifik (berdasarkan user_id) untuk dikirimi pesan pribadi.
4.  **Dashboard Manajemen**: Antarmuka di web admin untuk menulis, mengirim, dan memantau status pengiriman notifikasi.
5.  **Log Riwayat (Backend)**: Database server menyimpan setiap pesan yang dikirim beserta status keberhasilannya.
6.  **Riwayat Notifikasi (Mobile)**: Halaman khusus di aplikasi HP untuk melihat kembali pesan-pesan yang pernah masuk.
7.  **Auto-Trigger Notification**: Sistem otomatis mengirim notifikasi ke semua member saat ada foto baru yang diunggah dan disetujui di Galeri.

---

## 🚀 10 Saran Pengembangan Fitur Notifikasi (Premium)

1.  **Rich Notifications (Gambar)**: Menampilkan gambar preview langsung di dalam notifikasi push (misal: foto galeri baru muncul di banner notifikasi).
2.  **Notification Categories**: Pengguna bisa memilih jenis notifikasi apa yang ingin mereka terima (misal: hanya ingin notifikasi Jadwal, tapi mematikan notifikasi Galeri).
3.  **Actionable Notifications**: Tombol di dalam notifikasi (misal: tombol "Lihat Detail" atau "Bayar Kas" yang langsung membuka halaman terkait).
4.  **Pengingat Kas Otomatis**: Sistem secara otomatis mengirim push notification setiap minggu kepada member yang memiliki tunggakan uang kas.
5.  **Pengingat Jadwal (15 Menit Sebelum)**: Notifikasi otomatis yang muncul 15 menit sebelum mata kuliah dimulai berdasarkan jadwal hari itu.
6.  **Status Read/Unread**: Menampilkan tanda titik merah di aplikasi untuk notifikasi yang belum pernah dibuka oleh pengguna.
7.  **Scheduled Notifications**: Fitur bagi admin untuk menjadwalkan pengiriman pesan (misal: buat pesan sekarang untuk dikirim besok jam 8 pagi).
8.  **In-App Inbox**: Folder pesan di dalam aplikasi yang lebih lengkap, mendukung penghapusan pesan secara individual oleh pengguna.
9.  **Deep Linking**: Saat notifikasi diklik, aplikasi langsung membuka halaman spesifik (misal: klik notifikasi galeri langsung buka foto tersebut, bukan ke Dashboard).
10. **Quiet Hours (DND)**: Fitur bagi member untuk mengatur waktu "Jangan Ganggu" di mana aplikasi tidak akan membunyikan suara notifikasi (misal: jam 22.00 - 06.00).

---

> [!TIP]
> Prioritas selanjutnya yang disarankan adalah **Deep Linking** dan **Status Read/Unread** untuk meningkatkan keterlibatan pengguna dengan konten aplikasi.
