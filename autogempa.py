import time
import math
import requests
from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.align import Align

console = Console()

# Koordinat Kota Palu
PALU_LAT = -0.9003
PALU_LON = 119.8780

URLS = {
    "Gempa Terbaru": "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json",
    "Gempa M5+": "https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json",
    "Gempa Dirasakan": "https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/137.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )

    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def parse_coord(gempa):
    """
    BMKG biasanya punya:
    Coordinates: "-0.98,119.65"
    """
    coord = gempa.get("Coordinates", "")
    lat, lon = coord.split(",")
    return float(lat), float(lon)

def ambil_json(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

def ambil_semua_gempa():
    hasil = []

    for nama, url in URLS.items():
        try:
            data = ambil_json(url)
            isi = data.get("Infogempa", {})

            if isinstance(isi.get("gempa"), list):
                daftar = isi["gempa"]
            else:
                daftar = [isi["gempa"]]

            for g in daftar:
                g["_sumber"] = nama
                hasil.append(g)

        except Exception as e:
            hasil.append({
                "_error": True,
                "_sumber": nama,
                "error": str(e)
            })

    return hasil

def warna_magnitude(mag):
    try:
        m = float(str(mag).replace(",", "."))
    except:
        return "white"

    if m >= 6:
        return "bold red"
    elif m >= 5:
        return "yellow"
    elif m >= 4:
        return "cyan"
    return "green"

def buat_dashboard(gempa_list):
    table = Table(title="MONITOR GEMPA BMKG - FOKUS PALU / SULAWESI TENGAH", expand=True)

    table.add_column("Sumber", style="cyan", no_wrap=True)
    table.add_column("Tanggal / Jam", style="white")
    table.add_column("Magnitudo", justify="center")
    table.add_column("Kedalaman", justify="center")
    table.add_column("Jarak dari Palu", justify="center")
    table.add_column("Wilayah")
    table.add_column("Potensi / Dirasakan")

    data_valid = []

    for g in gempa_list:
        if g.get("_error"):
            table.add_row(
                g["_sumber"],
                "-",
                "-",
                "-",
                "-",
                f"[red]{g['error']}[/red]",
                "-"
            )
            continue

        try:
            lat, lon = parse_coord(g)
            jarak = haversine(PALU_LAT, PALU_LON, lat, lon)
        except:
            jarak = None

        wilayah = g.get("Wilayah", "-")
        potensi = g.get("Potensi", g.get("Dirasakan", "-"))
        mag = g.get("Magnitude", "-")

        # Filter utama: tampilkan yang dekat Palu atau menyebut Sulteng/Palu
        dekat_palu = jarak is not None and jarak <= 500
        teks_relevan = any(k.lower() in wilayah.lower() for k in [
            "palu", "sulawesi tengah", "sulteng", "donggala",
            "parigi", "poso", "sigi", "morowali", "toli-toli",
            "tolitoli", "mamuju"
        ])

        if dekat_palu or teks_relevan:
            data_valid.append((g, jarak))

    if not data_valid:
        table.add_row(
            "-",
            "-",
            "-",
            "-",
            "-",
            "[green]Belum ada data gempa relevan di sekitar Palu/Sulteng[/green]",
            "-"
        )
    else:
        data_valid.sort(key=lambda x: x[1] if x[1] is not None else 99999)

        for g, jarak in data_valid:
            mag = g.get("Magnitude", "-")
            tanggal_jam = f"{g.get('Tanggal', '-')}\n{g.get('Jam', '-')}"
            jarak_txt = f"{jarak:.1f} km" if jarak is not None else "-"

            table.add_row(
                g.get("_sumber", "-"),
                tanggal_jam,
                f"[{warna_magnitude(mag)}]{mag}[/]",
                g.get("Kedalaman", "-"),
                jarak_txt,
                g.get("Wilayah", "-"),
                g.get("Potensi", g.get("Dirasakan", "-"))
            )

    info = f"""
[bold]Lokasi Pantauan:[/] Kota Palu
[bold]Koordinat Palu:[/] {PALU_LAT}, {PALU_LON}
[bold]Update Lokal:[/] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
[bold]Interval:[/] 30 detik
"""

    return Panel(
        Align.center(table),
        title="[bold red]BMKG GEMPA CLI[/bold red]",
        subtitle=info,
        border_style="blue"
    )

def main():
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            gempa = ambil_semua_gempa()
            live.update(buat_dashboard(gempa))
            time.sleep(30)

if __name__ == "__main__":
    main()