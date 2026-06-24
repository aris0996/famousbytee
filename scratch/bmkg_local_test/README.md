# BMKG Local Test

Demo lokal terpisah untuk menguji konsep fitur cuaca, gempa, dan peringatan dini.

## Menjalankan

```powershell
python server.py
```

Buka:

```text
http://127.0.0.1:5055
```

Endpoint uji:

```text
http://127.0.0.1:5055/api/health
http://127.0.0.1:5055/api/regions
http://127.0.0.1:5055/api/locations
http://127.0.0.1:5055/api/locations?adm3=72.71.08
http://127.0.0.1:5055/api/local-info?adm3=72.71.08&location=72.71.08.1002
http://127.0.0.1:5055/api/coverage
```

## Catatan

- Data bersumber dari BMKG dan harus ditampilkan sebagai sumber di aplikasi.
- Cuaca BMKG memakai kode wilayah tingkat kelurahan/desa (`adm4`), bukan langsung kecamatan.
- Demo ini memakai cache 5 menit agar tidak terlalu sering meminta data ke BMKG.
- Daftar kecamatan yang dicakup: seluruh Kota Palu, Kabupaten Sigi, Kabupaten Donggala, dan Kabupaten Parigi Moutong sesuai data yang berhasil dibaca dari halaman BMKG.
- Endpoint `/api/coverage` akan mengecek jumlah desa/kelurahan yang tersedia untuk setiap kecamatan.
