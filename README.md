# Bot Telegram cek log ClickHouse

Bot menyediakan `/start` untuk melihat menu dan `/cek61 HH:MM-HH:MM` untuk
mengambil baris yang cocok dari 2000 baris terakhir log ClickHouse serta
journal systemd melalui SSH. Kedua sumber dianalisis bersama untuk menghasilkan
dugaan penyebab restart, tingkat keyakinan, dan baris bukti dalam blok kode.
Command grep log ClickHouse menyertakan konteks setelah baris yang cocok agar
stack trace exception dapat membantu diagnosis.

Jika log ClickHouse secara eksplisit mencatat `Memory limit exceeded`,
`OvercommitTracker`, atau pemakaian memori puncak/maksimum, hasil juga
menampilkan konteks penyebab OOM dan `query_id` bila tersedia. Jika journal
kernel mencatat proses ClickHouse dibunuh oleh OOM killer, bukti OOM killer
dan `oom_reaper` ditampilkan bersama; confidence heuristik untuk bukti
`Out of memory: Killed process ... (clickhouse...)` adalah 100/100. Konteks
query hanya ditampilkan bila tercatat dan tidak dengan sendirinya membuktikan
query tersebut sebagai penyebab langsung OOM atau restart.
Jika journal pada rentang yang diminta mencatat beberapa proses ClickHouse
terbunuh oleh OOM, setiap kejadian ditampilkan terpisah beserta waktu dan
bukti yang berdekatan; waktu diambil dari baris pembunuhan proses pada journal,
bukan dari log ClickHouse lama di luar rentang.

Contoh `/cek61 01:00-01:15` membaca journal dari pukul 01:00 sampai 01:15 pada
hari ini menurut waktu lokal server. Batas akhir mencakup seluruh menit yang
disebutkan. Rentang harus berada pada hari yang sama dan waktu akhir harus
setelah waktu mulai.

## Konfigurasi

Isi file `.env` di folder yang sama dengan `main.py`:

```env
server=hostname_atau_ip_server
username=username_ssh
password=password_ssh
token=token_bot_telegram
db_host=hostname_atau_ip_clickhouse
db_scheme=nama_database_clickhouse
db_port=8123
db_usn=username_clickhouse
db_pass=password_clickhouse
```

Bot menggunakan password SSH dari `.env` dan memeriksa host key server melalui
`known_hosts` sistem. Sebelum menjalankan bot, pastikan host key server sudah
terdaftar pada akun sistem yang menjalankan program, misalnya dengan terhubung
secara interaktif menggunakan SSH dan menerima host key yang sudah diverifikasi.
`db_scheme` adalah nama database ClickHouse (misalnya `anjay_mabar`), bukan
protokol jaringan. Query dikirim melalui HTTP interface ClickHouse pada
`db_host:db_port` (contoh port HTTP `8123`); `db_pass` boleh kosong jika user
ClickHouse tidak memakai password. Jika analisis restart menghasilkan
confidence tinggi (minimal 70/100) dan waktu kejadian ditemukan pada journal,
bot menjalankan query `system.query_views_log` untuk satu jam sebelum waktu
restart yang tercatat pada journal dan mengirim hasilnya sebagai CSV. Timestamp
journal menyertakan zona waktu dan dinormalisasi ke UTC untuk batas query agar
tidak bergantung pada timezone ClickHouse. Konfigurasi database bersifat
opsional; jika belum lengkap atau query gagal, ringkasan tetap dikirim dan
status evidence tambahan dicantumkan.

## Menjalankan

Gunakan Python 3.10 atau lebih baru:

```powershell
python -m pip install -r requirements.txt
python main.py
```

## Struktur proyek

- `main.py` menginisialisasi konfigurasi, aplikasi Telegram, dan handler.
- `ceklog/config.py` memuat serta memvalidasi konfigurasi dari `.env`.
- `ceklog/handlers.py` menangani command Telegram serta mengirim hasil panjang
  sebagai file teks dan evidence ClickHouse sebagai CSV.
- `ceklog/ssh.py` menjalankan command SSH untuk kedua sumber log dan query
  evidence melalui HTTP interface ClickHouse.
- `ceklog/analysis.py` menganalisis log ClickHouse secara heuristik.
- `ceklog/analysis_phase2.py` menggabungkan log ClickHouse dan journal untuk
  analisis bersama.

Ringkasan merupakan dugaan berdasarkan log ClickHouse, bukan penentuan pasti.
Untuk mengetahui apakah proses dihentikan oleh operator/service manager atau
OOM killer, cocokkan bukti dengan journal systemd dan log kernel pada waktu
restart.

Siapa pun yang dapat menghubungi bot dapat menjalankan `/cek61`. Jangan
membagikan username bot/token bot; hasilnya mencakup log server.
